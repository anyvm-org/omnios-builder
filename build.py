#!/usr/bin/env python3
"""base-builder/build.py - single-process builder, driving QEMU directly.

Replaces build.sh + vbox.sh + vbox.py. Self-contained: everything the previous
vbox.py exposed as a CLI subcommand is now a module-level function in this
file, and the build pipeline (formerly build.sh) is main() below.

Hooks live in hooks/<name>.py and are run via exec() in this module's
namespace, so they can call any of the VM-abstraction functions directly
(string, enter, waitForText, ...). This mirrors the old `. hooks/<name>.sh`
source semantics.

Key win: every step runs in this one Python process, so the console daemon
that used to need a detached subprocess (so it could survive across many short
`python3 vbox.py xxx` CLI calls) is now just a thread inside ConsoleSession.
No fork; no IPC; no shim.

Usage: python3 build.py conf/<name>.conf
"""

import base64
import concurrent.futures
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import platform
import urllib.request

HOME = os.path.expanduser("~")
HOST_ARCH = platform.machine()
SLIRP_PREFIX = "192.168.122."
DEVNULL = subprocess.DEVNULL


# ============================================================================
# (A) Small helpers
# ============================================================================

def env(name, default=""):
    v = os.environ.get(name)
    return v if v is not None else default


def is_linux():
    return platform.system() == "Linux"


def is_darwin():
    return platform.system() == "Darwin"


def log(msg):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def run(cmd, **kw):
    """Run a command list; never raises on non-zero. Returns CompletedProcess."""
    return subprocess.run(cmd, **kw)


def sh(cmdstr):
    """Run a shell command string; returns exit code."""
    return subprocess.call(cmdstr, shell=True)


def _run_quiet(cmd, **kw):
    """Run a noisy command (apt/brew/pip/etc.) silently; on non-zero exit dump
    the captured output so failures are still debuggable."""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        log("FAILED (rc=%d): %s" % (r.returncode, " ".join(map(str, cmd))))
        if r.stdout: log(r.stdout)
        if r.stderr: log(r.stderr)
    return r


def _sh_quiet(cmdstr):
    """Shell-string variant of _run_quiet."""
    r = subprocess.run(cmdstr, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        log("FAILED (rc=%d): %s" % (r.returncode, cmdstr))
        if r.stdout: log(r.stdout)
        if r.stderr: log(r.stderr)
    return r.returncode


def state(osname, suffix):
    return "%s.%s" % (osname, suffix)


def read_state(osname, suffix, default=""):
    try:
        with open(state(osname, suffix)) as f:
            return f.read().strip()
    except OSError:
        return default


def write_state(osname, suffix, value):
    with open(state(osname, suffix), "w") as f:
        f.write(str(value))


def read_pid(osname):
    try:
        return int(read_state(osname, "pid"))
    except ValueError:
        return 0


def pid_alive(pid):
    """True iff `pid` is a live process (NOT a zombie). `os.kill(pid, 0)`
    alone returns success for zombies (the PID stays in the table until its
    parent waits on it), which would make isRunning() insist a crashed/
    exited QEMU is still up and _wait_vm_down() loop forever. Reap our own
    zombie child if we can, then re-check via /proc/<pid>/status which
    exposes the actual state (R/S/D/Z/...)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False  # PID doesn't exist at all
    # Reap if this is our own zombie child (no-op otherwise).
    try:
        gone, _status = os.waitpid(pid, os.WNOHANG)
        if gone:
            return False
    except OSError:
        pass  # not our child or already reaped
    # Linux /proc/<pid>/status -- State is Z (zombie), X (dead), or a live state.
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("State:"):
                    parts = line.split()
                    state = parts[1] if len(parts) > 1 else ""
                    return state not in ("Z", "X")
    except OSError:
        pass
    return True


def free_port(start, end):
    for p in range(start, end + 1):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return 0


def vnc_server():
    """vncdotool '-s' target for this VM's VNC port. build_qemu_args picks a
    free 5900-5999 port per VM (write_state 'vncport') and binds QEMU's VNC
    display to it; we fall back to 5900 (display :0) when it isn't allocated
    yet. Pointing every vncdotool call at the right port lets several
    builders run on one host without colliding on the default VNC port."""
    osname = env("VM_OS_NAME")
    port = (read_state(osname, "vncport") if osname else "") or "5900"
    return ["-s", "127.0.0.1::%s" % port]


def tail_file(path, n):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


# ============================================================================
# (B) QEMU HMP monitor over TCP (was vbox.py:qmon)
# ============================================================================

def qmon(command, timeout=2.0):
    """Send one HMP command, return reply text or None. Ported from
    anyvm.py:_qmon_send. Never send 'quit' -- it terminates QEMU; we close the
    socket from our side and the server,nowait monitor keeps listening."""
    osname = env("VM_OS_NAME")
    if not osname:
        return None
    port = read_state(osname, "monport")
    if not port:
        return None
    try:
        port = int(port)
    except ValueError:
        return None
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    except OSError:
        return None
    chunks = []
    try:
        s.settimeout(timeout)
        s.sendall((command + "\n").encode("utf-8"))
        while True:
            try:
                data = s.recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()
    text = b"".join(chunks).decode("utf-8", "ignore")
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    return text.replace("\b", "").replace("\r", "")


# Pin slirp DHCP to a single address. The slirp built-in DHCP server hands
# out leases from `dhcpstart` up through .254; setting `dhcpstart=.254`
# leaves the guest exactly ONE address to take (.254 itself), so a fresh
# boot always gets the same IP regardless of any stale lease that might
# survive in the guest's /var. We then bake `-192.168.122.254:22` directly
# into every hostfwd entry instead of leaving the guest part blank
# (otherwise slirp forwards to the *first* address it saw, which can race
# with the guest's actual DHCP completion early in boot).
SLIRP_EXPECTED_GUEST_IP = SLIRP_PREFIX + "254"


def parse_usernet_ip(text):
    """Parse 'info usernet' output for the guest IP. Ported from
    anyvm.py:get_vm_ip_from_monitor (anyvm.py:3192-3222).

    Returns the slirp-net IP that originated the most outbound flows, or
    None if the usernet table has no outbound traffic from the guest yet
    (idle / not booted enough / headless arch with sleeping NIC)."""
    pat = re.compile(r'^\s*\w+\[[^\]]+\]\s+\d+\s+(\S+)\s+\d+\s+(\S+)\s+\d+', re.M)
    reserved = {0, 1, 2, 3, 255}
    cand = {}
    for m in pat.finditer(text):
        src, dst = m.group(1), m.group(2)
        if src.startswith(SLIRP_PREFIX) and not dst.startswith(SLIRP_PREFIX):
            try:
                last = int(src.rsplit(".", 1)[1])
            except ValueError:
                continue
            if last in reserved:
                continue
            cand[src] = cand.get(src, 0) + 1
    if cand:
        return max(cand.items(), key=lambda kv: kv[1])[0]
    return None


def parse_serial_log_ip(serial_path):
    """Detect the guest's DHCP-assigned IP by grepping the serial console log.
    Mirrors anyvm.py:get_vm_ip_from_serial (anyvm.py:3250-3283).

    Many BSDs print their lease on the console during boot (dhclient's
    "bound to <ip> -- renewal in ..."; rc.d/network's "inet <ip>"). This
    catches stale-lease cases on headless / -display none arches (riscv64,
    aarch64 console-build) where slirp's usernet table is still empty
    during boot-wait. Returns the most recent plausible IP or None."""
    if not serial_path or not os.path.exists(serial_path):
        return None
    try:
        with open(serial_path, "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    reserved = {0, 1, 2, 3, 255}
    prefix_re = re.escape(SLIRP_PREFIX)
    found = None
    for m in re.finditer(r"(?:bound to|inet)\s+(" + prefix_re + r"\d+)", data):
        ip = m.group(1)
        try:
            last = int(ip.rsplit(".", 1)[1])
        except ValueError:
            continue
        if last in reserved:
            continue
        found = ip
    return found


def rewrite_hostfwd_target(host_port, new_guest_ip, guest_port=22,
                           host_addr="127.0.0.1", proto="tcp"):
    """Rebind one hostfwd entry to point at a new guest IP via the QEMU
    monitor. Mirrors anyvm.py:rewrite_hostfwd_target (anyvm.py:3224-3248).

    The QEMU monitor returns no body on success and "Error" / "could not" on
    failure. Returns True only if both remove + add land cleanly."""
    rem = qmon("hostfwd_remove %s:%s:%s" % (proto, host_addr, host_port)) or ""
    add = qmon("hostfwd_add %s:%s:%s-%s:%s" % (
        proto, host_addr, host_port, new_guest_ip, guest_port)) or ""
    rem_ok = "Error" not in rem and "not found" not in rem.lower()
    add_ok = "Error" not in add and "could not" not in add.lower()
    return rem_ok and add_ok


def detect_guest_ip(osname=None):
    """Return the guest's actual DHCP-assigned IP, trying multiple detection
    methods in order. Returns '' if nothing is found yet.

    1. `info usernet` via the QEMU monitor -- works once the guest makes any
       outbound TCP connection (the strongest signal once boot's underway).
    2. Serial console log -- catches the dhclient "bound to <ip>" line which
       appears before any outbound traffic. Critical for headless / console
       arches where the usernet table is empty during boot-wait.

    Mirrors the layered probe in anyvm.py:6105-6118."""
    if osname is None:
        osname = env("VM_OS_NAME")
    if not osname:
        return ""
    ip = parse_usernet_ip(qmon("info usernet") or "") or ""
    if ip:
        return ip
    ip = parse_serial_log_ip(serial_log(osname)) or ""
    return ip


# ============================================================================
# (C) QEMU command construction (was vbox.py:build_qemu_args)
# ============================================================================

def qemu_bin_name():
    a = env("VM_ARCH") or "x86_64"
    if a == "aarch64": return "qemu-system-aarch64"
    if a == "riscv64": return "qemu-system-riscv64"
    if a == "sparc64": return "qemu-system-sparc64"
    if a in ("x86_64", "amd64"): return "qemu-system-x86_64"
    return "qemu-system-" + a


def resolve_qemu_bin():
    n = qemu_bin_name()
    return shutil.which(n) or n


def hvf_supported():
    if not is_darwin():
        return False
    try:
        out = subprocess.check_output(["sysctl", "-n", "kern.hv_support"], stderr=DEVNULL)
        return out.strip() == b"1"
    except Exception:
        return False


def kvm_ok():
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def qemu_accel():
    a = env("VM_ARCH") or "x86_64"
    if a == "riscv64":
        return "tcg"
    if a == "aarch64":
        if HOST_ARCH in ("aarch64", "arm64"):
            if kvm_ok(): return "kvm"
            if hvf_supported(): return "hvf"
        return "tcg"
    if HOST_ARCH in ("x86_64", "amd64"):
        if kvm_ok(): return "kvm"
        if hvf_supported(): return "hvf"
    return "tcg"


# OpenBSD releases whose aarch64/amd64 install media drive e1000 reliably but
# not virtio. After 7.6 the OpenBSD aarch64 builds switched to virtio-net.
# Mirrors anyvm.py:OPENBSD_E1000_RELEASES (anyvm.py:110).
OPENBSD_E1000_RELEASES = {"7.3", "7.4", "7.5", "7.6"}


def net_card():
    """Network device for -device.

    Mirrors anyvm.py:5185-5214 exactly. An explicit VM_NIC in the conf
    (libvirt-style model name like `virtio` / `e1000`) wins; otherwise the
    default is arch-aware (aarch64 -> virtio, else e1000), with per-OS
    overrides for guests whose installed kernel only ships the other driver:

      * openbsd 7.3..7.6  -> e1000  (older arm64 kernels lack vio(4))
      * openbsd >=7.7     -> virtio-net-pci
      * dragonflybsd != 6.4.0 -> virtio-net-pci
      * riscv64           -> virtio-net-pci  (RISC-V virt has no e1000 PCI BAR)
      * netbsd aarch64    -> virtio-net-pci  (evbarm GENERIC has vioif, not wm)
      * freebsd           -> virtio-net-pci  (cloud images: vtnet0 baked in)
      * ubuntu            -> virtio-net-pci  (cloud-init/netplan pinned to virtio)
    """
    n = env("VM_NIC")
    if n:
        return "virtio-net-pci" if n in ("virtio", "virtio-net") else n

    arch = env("VM_ARCH") or "x86_64"
    osname = env("VM_OS_NAME")
    release = env("VM_RELEASE")

    nic = "virtio-net-pci" if arch == "aarch64" else "e1000"
    if osname == "openbsd" and release:
        release_base = release.split("-")[0]
        nic = "e1000" if release_base in OPENBSD_E1000_RELEASES else "virtio-net-pci"
    elif osname == "dragonflybsd":
        if release != "6.4.0":
            nic = "virtio-net-pci"
    elif arch == "riscv64":
        nic = "virtio-net-pci"
    elif osname == "netbsd" and arch == "aarch64":
        nic = "virtio-net-pci"
    elif osname == "freebsd":
        nic = "virtio-net-pci"
    elif osname == "ubuntu":
        nic = "virtio-net-pci"
    return nic


# OpenBSD/amd64 release suffixes that imply a desktop image (X11). Mirrors
# anyvm.py:4437. These releases get -vga cirrus because xenocara's only
# working QEMU driver on amd64 is xf86-video-cirrus.
_OPENBSD_DESKTOP_SUFFIXES = (
    "-xfce", "-gnome", "-kde", "-kde6", "-mate",
    "-lxqt", "-lumina", "-enlightenment", "-cinnamon",
)


def vga_type():
    """Decide the QEMU video device. Mirrors anyvm.py:4430-4458, plus
    anyvm.py:5304-5306 (aarch64 virtio -> virtio-gpu-pci) and 5441-5444
    (x86 -> -vga argument).

    The conf can pin a value via VM_VGA (libvirt-style: std / virtio /
    cirrus / virtio-gpu / qxl); when unset the OS-aware default is:

      * netbsd amd64           -> std   (NetBSD has no DRM for virtio-gpu)
      * haiku                  -> std   (its driver doesn't match virtio-vga)
      * openbsd amd64 desktop  -> cirrus (xenocara only ships xf86-video-cirrus)
      * else                   -> virtio

    `virtio` is normalized to `virtio-vga` on x86 and `virtio-gpu-pci` on
    aarch64 by the caller that emits `-vga` vs `-device`.
    """
    v = env("VM_VGA")
    if v:
        return v
    osname = env("VM_OS_NAME")
    arch = env("VM_ARCH") or "x86_64"
    release = env("VM_RELEASE") or ""
    if osname == "netbsd" and arch != "aarch64":
        return "std"
    if osname == "haiku":
        return "std"
    if (osname == "openbsd" and arch != "aarch64"
            and any(release.endswith(s) for s in _OPENBSD_DESKTOP_SUFFIXES)):
        return "cirrus"
    return "virtio"


def disk_if():
    """Disk bus for -drive if=. VM_DISK may carry extra attrs; keep only the
    leading bus token (so 'virtio,discard=unmap' -> 'virtio').

    When VM_DISK is unset, the OS-aware default mirrors anyvm.py:5058-5081 so a
    builder whose conf forgets VM_DISK still gets the right enumeration in the
    guest:

      * dragonflybsd -> ide  (guest BIOS bootloader pinned to ad0)
      * ghostbsd     -> ide  (pc-sysinstall + ada0 + SeaBIOS)
      * tribblix     -> sata (live_install.sh installs to c2t0d0 via AHCI)
      * else         -> virtio
    """
    raw = env("VM_DISK")
    if raw:
        d = raw.split(",", 1)[0]
        return d or "virtio"
    osname = env("VM_OS_NAME")
    if osname in ("dragonflybsd", "ghostbsd"):
        return "ide"
    if osname == "tribblix":
        return "sata"
    return "virtio"


def obsd_acpi_off():
    """openbsd aarch64 < 7.4 needs acpi=off for FDT PCI routing."""
    if env("VM_OS_NAME") != "openbsd" or env("VM_ARCH") != "aarch64":
        return False
    rel = (env("VM_RELEASE") or "").split("-", 1)[0].split(".")
    try:
        maj = int(rel[0]); mn = int(rel[1]) if len(rel) > 1 else 0
    except (ValueError, IndexError):
        return False
    return (maj, mn) < (7, 4)


def make_blank(path, mb):
    with open(path, "wb") as f:
        f.truncate(mb * 1024 * 1024)


def copy_into(src, dst):
    with open(src, "rb") as s: data = s.read()
    with open(dst, "r+b") as d: d.seek(0); d.write(data)


def _aarch64_efi_search_dirs(qemu_bin=None):
    """Mirrors anyvm.py:5224-5231. Search next to the qemu binary first
    (relocated qemu installs honored), then the system paths."""
    dirs = []
    if qemu_bin:
        try:
            qpref = os.path.dirname(os.path.dirname(os.path.realpath(qemu_bin)))
            dirs.append(os.path.join(qpref, "share"))
        except Exception:
            pass
    dirs += ["/usr/share", "/opt/homebrew/share", "/usr/local/share"]
    return dirs


# Relative paths under each search dir where aarch64 EDK2 CODE firmware lives.
# Mirrors anyvm.py:5232-5238. Order = preference (Debian/Ubuntu first).
_AARCH64_EFI_CODE_RELNAMES = [
    os.path.join("edk2", "aarch64", "QEMU_EFI.fd"),
    os.path.join("qemu-efi-aarch64", "QEMU_EFI.fd"),
    os.path.join("AAVMF", "AAVMF_CODE.fd"),
    os.path.join("qemu", "edk2-aarch64-code.fd"),
    os.path.join("edk2", "aarch64", "QEMU_EFI-pflash.raw"),
]


def _find_aarch64_efi_code(qemu_bin=None):
    """Locate the aarch64 EDK2 CODE firmware. Returns full path or ''."""
    for d in _aarch64_efi_search_dirs(qemu_bin):
        for rn in _AARCH64_EFI_CODE_RELNAMES:
            p = os.path.join(d, rn)
            if os.path.exists(p):
                return p
    return ""


def _find_aarch64_efi_vars(code_src):
    """Find the VARS template matching the given CODE firmware. anyvm.py:5272-5288.

    EDK2 ships a pre-formatted NVRAM template next to the CODE blob; using a
    blank vars region instead crashes NetBSD evbarm + (sometimes) other guests
    with SetVariable-NVRAM-init failures that present as reboot loops at the
    [1.000xxx] kernel mark. Try matching templates by name; return '' if none."""
    if not code_src:
        return ""
    d = os.path.dirname(code_src)
    base = os.path.basename(code_src)
    guesses = []
    for a, b in [("QEMU_EFI", "QEMU_VARS"), ("_CODE", "_VARS"),
                 ("-code", "-vars"), ("CODE", "VARS")]:
        if a in base:
            guesses.append(os.path.join(d, base.replace(a, b)))
    guesses += [
        os.path.join(d, "QEMU_VARS.fd"),
        os.path.join(d, "vars-template-pflash.raw"),
        os.path.join(d, "AAVMF_VARS.fd"),
    ]
    for g in guesses:
        if g != code_src and os.path.exists(g):
            return g
    return ""


# Legacy alias kept for any in-tree references.
AARCH64_EFI_CANDIDATES = []


def build_qemu_args(media_kind=None, media_path=None):
    """Build the full QEMU argv. media_kind is None / 'cdrom' / 'disk'."""
    osname = env("VM_OS_NAME")
    arch = env("VM_ARCH") or "x86_64"
    qcow = "%s.qcow2" % osname
    sshport = read_state(osname, "sshport") or "22"

    monport = free_port(4444, 4544); write_state(osname, "monport", monport)
    serport = free_port(7000, 9000); write_state(osname, "serport", serport)
    vncport = free_port(5900, 5999); write_state(osname, "vncport", vncport)
    serlog = "%s.serial.log" % osname
    try: os.remove(serlog)
    except OSError: pass

    accel = qemu_accel()
    nic = net_card()
    dif = disk_if()
    console = bool(env("VM_USE_CONSOLE_BUILD"))

    a = []
    a += ["-chardev", "socket,id=serial0,host=127.0.0.1,port=%s,server=on,wait=off,logfile=%s" % (serport, serlog)]
    a += ["-serial", "chardev:serial0"]
    a += ["-monitor", "tcp:127.0.0.1:%s,server,nowait,nodelay" % monport]
    # RTC: `driftfix=slew` matches libvirt's `<timer name='rtc' tickpolicy='catchup'/>`
    # -- when KVM drops RTC interrupts under load, slew the guest clock forward
    # instead of dropping ticks (illumos / older BSDs hate ticks vanishing).
    # Haiku and Windows read the RTC as local time, not UTC; using UTC there
    # offsets the wall clock by the timezone (TLS / cert breakage). Mirrors
    # anyvm.py:5173-5176.
    rtc_base = "localtime" if osname in ("windows", "haiku") else "utc"
    a += ["-name", osname, "-m", "6144", "-smp", env("VM_CPU") or "2",
          "-rtc", "base=%s,clock=host,driftfix=slew" % rtc_base]
    # slirp DHCP pinned to .254 (see SLIRP_EXPECTED_GUEST_IP for rationale).
    # hostfwd target is the explicit guest IP so port forwarding never races
    # the guest's DHCP-bound state.
    a += ["-netdev", "user,id=net0,net=192.168.122.0/24,host=192.168.122.1,"
          "dhcpstart=192.168.122.254,ipv6=off,"
          "hostfwd=tcp:127.0.0.1:%s-192.168.122.254:22" % sshport]
    # virtio-rng-pci for all guests EXCEPT solaris -- Solaris does not have
    # a virtio-rng driver and the unrecognized device disrupts early boot.
    # Mirrors anyvm.py:5627.
    if osname != "solaris":
        a += ["-object", "rng-builtin,id=rng0",
              "-device", "virtio-rng-pci,rng=rng0,max-bytes=1024,period=1000"]

    if arch == "aarch64":
        efi = "%s-QEMU_EFI.fd" % osname
        varsf = "%s-QEMU_EFI_VARS.fd" % osname
        code_src = _find_aarch64_efi_code(resolve_qemu_bin())
        if not os.path.exists(efi):
            if not code_src:
                log("aarch64 UEFI CODE firmware not found "
                    "(install edk2-aarch64 / qemu-efi-aarch64)")
            make_blank(efi, 64)
            if code_src:
                copy_into(code_src, efi)
        if not os.path.exists(varsf):
            make_blank(varsf, 64)
            # Crucial: NetBSD evbarm + (sometimes) other aarch64 guests reboot
            # in a 3-second loop on a completely blank vars pflash because EDK2
            # cannot initialize NVRAM. Copy the matching VARS template instead.
            vars_src = _find_aarch64_efi_vars(code_src)
            if vars_src:
                log("aarch64 vars template: %s" % vars_src)
                copy_into(vars_src, varsf)
            else:
                log("aarch64 UEFI VARS template not found; using blank store "
                    "(this can cause guest reboot loops on NetBSD evbarm)")
        mopts = "virt,accel=%s,gic-version=3,usb=on" % accel
        if obsd_acpi_off(): mopts += ",acpi=off"
        if accel in ("kvm", "hvf"): cpu = "host"
        elif env("VM_OS_NAME") == "openbsd": cpu = "neoverse-n1"
        else: cpu = "max"
        a += ["-machine", mopts, "-cpu", cpu]
        a += ["-device", "qemu-xhci", "-device", "%s,netdev=net0" % nic]
        a += ["-drive", "if=pflash,format=raw,readonly=on,file=%s" % efi]
        a += ["-drive", "if=pflash,format=raw,file=%s,unit=1" % varsf]
        # VGA device. anyvm.py:5304-5306 -- 'virtio' / 'virtio-gpu' normalize
        # to 'virtio-gpu-pci' on aarch64 (the only working PCI VGA model on
        # the virt machine). std on aarch64 has no QEMU implementation.
        vga = vga_type()
        if vga in ("virtio", "virtio-gpu", "std", ""):
            vga = "virtio-gpu-pci"
        a += ["-device", vga]
        a += ["-drive", "file=%s,format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap" % qcow]
        if media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0" % media_path]
            a += ["-device", "virtio-blk-pci,drive=inst0,bootindex=0"]
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=1"]
        elif media_kind == "cdrom":
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0,media=cdrom" % media_path]
            a += ["-device", "usb-storage,drive=inst0,bootindex=0"]
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=1"]
        else:
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=0"]
        if not console:
            a += ["-device", "usb-kbd", "-device", "virtio-tablet-pci"]

    elif arch == "riscv64":
        a += ["-machine", "virt,accel=tcg,usb=on,acpi=off", "-cpu", "rv64"]
        a += ["-device", "qemu-xhci", "-device", "%s,netdev=net0" % nic,
              "-device", "virtio-balloon-pci"]
        a += ["-kernel", "/usr/lib/u-boot/qemu-riscv64_smode/u-boot.bin"]
        if media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=virtio" % media_path]
        elif media_kind == "cdrom":
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0,media=cdrom" % media_path]
            a += ["-device", "usb-storage,drive=inst0"]
        a += ["-drive", "file=%s,format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap" % qcow]

    elif arch == "sparc64":
        a += ["-machine", "sun4u", "-device", "%s,netdev=net0" % nic]
        a += ["-drive", "file=%s,format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap" % qcow]
        if media_kind == "cdrom":
            a += ["-cdrom", media_path, "-boot", "order=dc"]
        elif media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=ide" % media_path]

    else:
        # x86_64 (and any other PC-class arch).
        a += ["-machine", "pc,accel=%s,hpet=off,smm=off,graphics=on,vmport=off,usb=on" % accel]
        # Mirrors anyvm.py:5413-5439.
        if accel == "kvm":
            if osname == "dragonflybsd":
                # DragonFlyBSD's early-boot init writes to MSRs that vary by
                # runner CPU generation. -cpu host exposes too much; even
                # pmu=off was not enough to stop intermittent #GP-in-wrmsr
                # right after TSC calibration. Lock to a stable named model
                # so guest CPUID is identical across all runner hardware.
                # Note: named CPU models do NOT support `migratable` or
                # `l3-cache` properties (those are -cpu host only).
                cpu = "Broadwell-v4,+hypervisor,+invtsc"
            else:
                cpu = "host,kvm=on,l3-cache=on,+hypervisor,migratable=no,+invtsc"
        elif accel == "hvf":
            cpu = "host,+rdrand,+rdseed"
        else:
            cpu = "qemu64,+rdrand,+rdseed"
        # Disable the guest PMU. Exposing the host PMU via -cpu host can
        # trigger intermittent #GP-in-wrmsr crashes during early guest boot
        # (notably DragonFlyBSD) when the runner CPU generation exposes PMU
        # MSRs that KVM refuses writes to.
        if accel in ("kvm", "hvf"):
            cpu += ",pmu=off"
        a += ["-cpu", cpu]
        # PIT lost-tick policy: libvirt-era XML carried
        # `<timer name='pit' tickpolicy='delay'/>`. Without it, QEMU's KVM
        # PIT backend drops lost interrupts, which illumos kernels (OmniOS,
        # OpenIndiana, Solaris) interpret as the system stalling and either
        # spin or hang. `delay` queues missed ticks instead. Cheap to set
        # for every x86 guest; only the KVM backend cares about the global,
        # so it's a no-op under TCG / HVF.
        if accel == "kvm":
            a += ["-global", "kvm-pit.lost_tick_policy=delay"]
        a += ["-device", "%s,netdev=net0" % nic, "-device", "virtio-balloon-pci"]
        # Pin the install CDROM (when present) to IDE primary master,
        # matching the libvirt-era release XML (`<target dev='hda' bus='ide'/>`).
        # That makes the guest see it as cd0a -- NetBSD sysinst's default
        # mount path. QEMU's `-cdrom` shortcut puts it at IDE index=2
        # (secondary master = cd1a), which NetBSD then fails to mount and
        # falls into the "Distribution medium" menu loop.
        # When the main disk is also on IDE (dragonflybsd / ghostbsd:
        # VM_DISK=ide), shift it to secondary master to avoid colliding
        # with the cdrom slot.
        if dif == "sata":
            a += ["-drive", "file=%s,format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap" % qcow]
            a += ["-device", "ich9-ahci,id=ahci0", "-device", "ide-hd,bus=ahci0.0,drive=disk0"]
        elif dif == "ide" and media_kind == "cdrom":
            a += ["-drive", "file=%s,format=qcow2,if=ide,index=2,discard=unmap,detect-zeroes=unmap" % qcow]
        else:
            a += ["-drive", "file=%s,format=qcow2,if=%s,discard=unmap,detect-zeroes=unmap" % (qcow, dif)]
        if media_kind == "cdrom":
            a += ["-drive", "file=%s,format=raw,if=ide,index=0,media=cdrom" % media_path,
                  "-boot", "order=dc,menu=off"]
        elif media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=ide" % media_path]
        # VGA device, anyvm.py:5441-5444. NetBSD/Haiku -> std,
        # OpenBSD desktop -> cirrus, default -> virtio (= virtio-vga on x86).
        a += ["-vga", vga_type()]

    # VNC display number = port - 5900 (display :N <-> TCP 5900+N).
    a += ["-display", "vnc=127.0.0.1:%d" % (vncport - 5900)]
    if not console and arch != "aarch64":
        a += ["-device", "usb-tablet"]
    return a


def launch_qemu(media_kind=None, media_path=None):
    """Launch QEMU detached so it survives this Python process."""
    osname = env("VM_OS_NAME")
    qbin = resolve_qemu_bin()
    cmd = [qbin] + build_qemu_args(media_kind, media_path)
    with open(state(osname, "cmdline"), "w") as f:
        f.write(" ".join(cmd) + "\n")
    log("Launching QEMU for %s:" % osname)
    log(" ".join(cmd))
    logf = open("%s.qemu.log" % osname, "ab")
    p = subprocess.Popen(cmd, stdin=DEVNULL, stdout=logf, stderr=logf,
                         start_new_session=True)
    write_state(osname, "pid", p.pid)
    time.sleep(1)
    if p.poll() is not None:
        log("QEMU failed to start for %s; tail of %s.qemu.log:" % (osname, osname))
        log(tail_file("%s.qemu.log" % osname, 50))
        return 1
    log("QEMU started: pid=%d vnc=127.0.0.1::%s monitor=%s serial=%s"
        % (p.pid, read_state(osname, "vncport"), read_state(osname, "monport"),
           read_state(osname, "serport")))
    return 0


# ============================================================================
# (D) Multi-threaded HTTP downloader (replaces axel)
# ============================================================================

DL_THREADS = 8
DL_CHUNK_MIN = 4 * 1024 * 1024
DL_BUF = 1024 * 1024
DL_CONN_TIMEOUT = 60
DL_READ_TIMEOUT = 600
DL_ATTEMPTS = 3
DL_USER_AGENT = "build.py/1 (+anyvm)"


def _http_probe(url):
    try:
        req = urllib.request.Request(
            url, headers={"Range": "bytes=0-0", "User-Agent": DL_USER_AGENT})
        with urllib.request.urlopen(req, timeout=DL_CONN_TIMEOUT) as resp:
            status = resp.getcode()
            cr = resp.headers.get("Content-Range")
            cl = resp.headers.get("Content-Length")
            if status == 206 and cr and "/" in cr:
                try: return int(cr.rsplit("/", 1)[1]), True
                except ValueError: pass
            if cl is not None:
                try: return int(cl), False
                except ValueError: pass
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": DL_USER_AGENT})
        with urllib.request.urlopen(req, timeout=DL_CONN_TIMEOUT) as resp:
            cl = resp.headers.get("Content-Length")
            ar = (resp.headers.get("Accept-Ranges") or "").lower()
            size = int(cl) if cl is not None else None
            return size, (ar == "bytes")
    except Exception:
        return None, False


def _http_get_stream(url, start=None, end=None):
    headers = {"User-Agent": DL_USER_AGENT}
    if start is not None:
        headers["Range"] = "bytes=%d-" % start if end is None else "bytes=%d-%d" % (start, end)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=DL_READ_TIMEOUT)


def _download_chunk(url, fpath, start, end):
    last_err = None
    for attempt in range(DL_ATTEMPTS):
        try:
            with _http_get_stream(url, start, end) as resp, open(fpath, "r+b") as f:
                f.seek(start)
                while True:
                    buf = resp.read(DL_BUF)
                    if not buf: break
                    f.write(buf)
            return
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err if last_err else RuntimeError("download failed")


def _download_single(url, fpath):
    with _http_get_stream(url) as resp, open(fpath, "wb") as f:
        while True:
            buf = resp.read(DL_BUF)
            if not buf: break
            f.write(buf)


def download(link=None, fileout=None):
    """Multi-threaded HTTP downloader. 8 parallel Range requests when the
    server supports byte ranges; single-stream otherwise."""
    if not fileout:
        log("Usage: download link localfile"); return 1
    log("Downloading %s" % link)
    size, ranges_ok = _http_probe(link)
    if size is None or not ranges_ok or size < DL_CHUNK_MIN:
        try: _download_single(link, fileout)
        except Exception as e:
            log("download failed: %s" % e); return 1
        log("Download finished"); return 0
    with open(fileout, "wb") as f: f.truncate(size)
    chunk = (size + DL_THREADS - 1) // DL_THREADS
    pieces = []
    for i in range(DL_THREADS):
        s = i * chunk
        if s >= size: break
        e = min(s + chunk, size) - 1
        pieces.append((s, e))
    log("size=%d, %d threads, chunk=%d bytes" % (size, len(pieces), chunk))
    rc = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pieces)) as ex:
        futs = [ex.submit(_download_chunk, link, fileout, s, e) for (s, e) in pieces]
        for fut in concurrent.futures.as_completed(futs):
            try: fut.result()
            except Exception as e:
                log("chunk failed: %s" % e); rc = 1
    if rc == 0:
        try:
            actual = os.path.getsize(fileout)
            if actual != size:
                log("size mismatch: expected %d, got %d" % (size, actual))
                rc = 1
        except OSError: pass
    if rc == 0: log("Download finished")
    return rc


# ============================================================================
# (E) Console session (was screen + nc; now an in-process thread)
# ============================================================================
#
# This is the central win of moving everything into one process:
# what used to be a detached daemon subprocess (so it could survive multiple
# `python3 vbox.py xxx` CLI calls) is now just a thread inside a
# ConsoleSession kept in a module-level dict, keyed by osname. The dict and
# the running QEMU socket live as long as this Python process does.
#
# Three roles the old `screen -dmLS NAME -L -Logfile FILE nc 127.0.0.1
# <serport>` covered:
#   (1) hold a long-lived TCP connection to QEMU's serial socket  -> self.ser
#   (2) record every byte the guest emits to a log file           -> reader thread
#   (3) provide a re-entrant input channel (`screen -X stuff`)    -> send()

def serial_log(osname):
    """The QEMU-written serial log file. Set up by build_qemu_args() via
    `-chardev socket,...,logfile=...` and truncated each launch_qemu()."""
    return "%s.serial.log" % osname


_console_sessions = {}     # osname -> ConsoleSession
_console_sessions_lock = threading.Lock()


class ConsoleSession(object):
    """Holds the persistent host-side connection to QEMU's serial socket so
    string()/enter()/... can inject bytes whenever they want.

    QEMU writes the full serial byte stream to <osname>.serial.log on its own
    (chardev logfile=), so we don't persist it again. But we still need to
    *drain* the socket continuously: the chardev is full-duplex and QEMU will
    keep writing guest output to it; if no one reads, the host-side TCP buffer
    fills up and the guest console eventually blocks. The drain thread reads
    and discards."""

    def __init__(self, serport):
        self.ser = socket.create_connection(("127.0.0.1", serport), timeout=5.0)
        self.ser.settimeout(1.0)
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._t = threading.Thread(target=self._drain, daemon=True,
                                   name="console-drain-%s" % (env("VM_OS_NAME") or "vm"))
        self._t.start()

    def _drain(self):
        try:
            while not self._stop.is_set():
                try:
                    data = self.ser.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break  # QEMU closed (VM gone)
        finally:
            self._stop.set()

    def send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        with self._send_lock:
            try:
                self.ser.sendall(data)
            except OSError:
                self._stop.set()

    def close(self):
        self._stop.set()
        try:
            self.ser.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.ser.close()
        except OSError:
            pass
        self._t.join(timeout=2.0)


def _send_console(s):
    """Inject bytes into the guest's console (console-build mode only)."""
    osname = env("VM_OS_NAME")
    if not osname: return
    with _console_sessions_lock:
        sess = _console_sessions.get(osname)
    if sess:
        sess.send(s)


def openConsole():
    """Open the host-side serial connection used by string()/enter()/... in
    console-build mode. No-op in VNC mode -- QEMU already serves VNC on :0."""
    osname = _check_osname("openConsole")
    if not osname: return 1
    if env("VM_USE_CONSOLE_BUILD"):
        closeConsole()
        try:
            serport = int(read_state(osname, "serport"))
        except ValueError:
            log("openConsole: no serport for %s" % osname); return 1
        try:
            sess = ConsoleSession(serport)
        except OSError as e:
            log("openConsole: cannot connect to serial 127.0.0.1:%d (%s)" % (serport, e))
            return 1
        with _console_sessions_lock:
            _console_sessions[osname] = sess
    return 0


def closeConsole():
    osname = env("VM_OS_NAME")
    if not osname: return 0
    with _console_sessions_lock:
        sess = _console_sessions.pop(osname, None)
    if sess:
        sess.close()
    return 0


# ============================================================================
# (F) setup (apt/brew)
# ============================================================================

def setup(install_ocr=None):
    """Install host dependencies. All package-manager output is captured and
    only printed on failure -- normal runs stay quiet."""
    log("setup: installing host dependencies (silent unless something fails)")
    if is_linux():
        apt_env = dict(os.environ)
        apt_env["DEBIAN_FRONTEND"] = "noninteractive"
        _run_quiet(["sudo", "-E", "apt-get", "update", "-qq"], env=apt_env)
        _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-qq",
                    "zstd", "qemu-utils", "qemu-system-x86", "ovmf", "expect",
                    "sshpass", "netcat-openbsd"], env=apt_env)
        if install_ocr:
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-qq",
                        "tesseract-ocr", "python3-pil",
                        "tesseract-ocr-eng", "tesseract-ocr-script-latn",
                        "python3-opencv", "python3-pip"], env=apt_env)
            if _sh_quiet("pip3 install -q --break-system-packages "
                         "pytesseract opencv-python vncdotool") != 0:
                _sh_quiet("pip3 install -q pytesseract opencv-python vncdotool")
            vp = os.path.join(HOME, ".local", "bin", "vncdotool")
            if os.path.exists(vp):
                _run_quiet(["sudo", "ln", "-sf", vp, "/usr/local/bin/vncdotool"])
        if env("VM_ARCH") == "riscv64":
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-qq",
                        "qemu-system-misc", "u-boot-qemu"], env=apt_env)
        if env("VM_ARCH") == "aarch64":
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-qq",
                        "qemu-system-arm", "qemu-efi-aarch64"], env=apt_env)
        # Make /dev/kvm usable by the current shell user. On GitHub Actions
        # runners (and most desktop distros) the device is mode crw-rw----
        # root:kvm and the runner / login user is NOT in the kvm group, so
        # qemu_accel() falls back to tcg even when KVM is available. The
        # libvirt-era builder didn't hit this because virt-install ran the
        # guest as the libvirt-qemu / qemu system user which IS in kvm; raw
        # QEMU runs as us, so we have to open the device ourselves.
        #
        # Best-effort: a developer running build.py locally may not have
        # passwordless sudo (or any sudo at all). In that case we just warn
        # and let qemu_accel() fall back to tcg -- the build still works,
        # only slower. Use `sudo -n` so we never block on a password prompt.
        if os.path.exists("/dev/kvm") and not os.access("/dev/kvm",
                                                       os.R_OK | os.W_OK):
            try:
                r = subprocess.run(["sudo", "-n", "chmod", "666", "/dev/kvm"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and os.access("/dev/kvm",
                                                   os.R_OK | os.W_OK):
                    log("setup: chmod 666 /dev/kvm -- KVM acceleration enabled")
                else:
                    log("setup: cannot relax /dev/kvm permissions "
                        "(no passwordless sudo, or user lacks privilege); "
                        "KVM unavailable, falling back to TCG. To enable KVM, "
                        "add this user to the 'kvm' group or run "
                        "`sudo chmod 666 /dev/kvm` manually before building.")
            except (subprocess.TimeoutExpired, OSError) as e:
                log("setup: chmod /dev/kvm attempt failed (%s); "
                    "KVM unavailable, falling back to TCG." % e)
    else:
        _run_quiet(["brew", "install", "tesseract", "qemu"])
        _sh_quiet("pip3 install -q pytesseract opencv-python vncdotool")
        log("Reloading sshd services in the Host")
        _sh_quiet('sudo sh -c \'echo "" >>/etc/ssh/sshd_config; '
                  'echo "StrictModes no" >>/etc/ssh/sshd_config\'')
        _run_quiet(["sudo", "launchctl", "unload",
                    "/System/Library/LaunchDaemons/ssh.plist"])
        _run_quiet(["sudo", "launchctl", "load", "-w",
                    "/System/Library/LaunchDaemons/ssh.plist"])
    os.makedirs(os.path.join(HOME, ".ssh"), exist_ok=True)
    os.chmod(os.path.join(HOME, ".ssh"), 0o700)
    _run_quiet(["sudo", "chmod", "755", HOME])
    log("setup: done")
    return 0


# ============================================================================
# (G) VM lifecycle
# ============================================================================

def createVM(isolink=None, ostype=None, sshport=None, disklink=None):
    osname = _check_osname("createVM")
    if not osname: return 1
    vdi = "%s.qcow2" % osname
    iso = "%s.iso" % osname
    if isolink.endswith("img"):
        iso = "%s.img" % osname
    if not os.path.exists(iso):
        download(isolink, iso)
        if isolink.endswith("bz2"):
            os.rename(iso, iso + ".bz2")
            sh("bzip2 -dc %s > %s" % (shlex.quote(iso + ".bz2"), shlex.quote(iso)))
    if disklink:
        if not os.path.exists(vdi):
            download(disklink, vdi)
    else:
        run(["qemu-img", "create", "-f", "qcow2", "-o", "preallocation=off", vdi, "200G"])
    try: os.chmod(vdi, 0o777)
    except OSError: pass
    write_state(osname, "sshport", sshport or "22")
    if iso.endswith("img"):
        return launch_qemu("disk", iso)
    return launch_qemu("cdrom", iso)


def createVMFromVHD(ostype=None, sshport=None):
    osname = _check_osname("createVMFromVHD")
    if not osname: return 1
    vhd = "%s.qcow2" % osname
    run(["qemu-img", "resize", vhd, "+200G"])
    write_state(osname, "sshport", sshport or "22")
    log("createVMFromVHD: %s prepared (sshport=%s). startVM will boot it."
        % (vhd, sshport))
    return 0


def startVM():
    if not _check_osname("startVM"): return 1
    return launch_qemu()


def shutdownVM():
    if not _check_osname("shutdownVM"): return 1
    qmon("system_powerdown")
    time.sleep(2)
    return 0


def destroyVM():
    osname = _check_osname("destroyVM")
    if not osname: return 1
    pid = read_pid(osname)
    if pid_alive(pid):
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
        for _ in range(10):
            if not pid_alive(pid): break
            time.sleep(1)
        if pid_alive(pid):
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
    try: os.remove(state(osname, "pid"))
    except OSError: pass
    time.sleep(2)
    return 0


def isRunning():
    """Silent check; returns 0 if VM running, 1 otherwise. Use _wait_vm_down()
    in pipelines so wait loops log periodic progress."""
    osname = env("VM_OS_NAME")
    if not osname: return 1
    return 0 if pid_alive(read_pid(osname)) else 1


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _serial_tail_line(window=4096):
    """Return (size, last_line) of the QEMU serial log: total bytes plus the
    last non-empty line (ANSI escape sequences and control bytes stripped).
    Used to show what the guest is actually doing during long waits."""
    osname = env("VM_OS_NAME")
    if not osname: return 0, ""
    path = "%s.serial.log" % osname
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - window))
            buf = f.read()
    except OSError:
        return 0, ""
    text = buf.decode("utf-8", "replace")
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    last = ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            last = line
            break
    return size, last


def _wait_vm_down(what="VM", poll=20):
    """Block until isRunning() reports not-running. Every poll prints a one-
    line status: elapsed time, size of <osname>.serial.log, and the last non-
    empty line of the guest console -- so it's obvious whether the install is
    making progress or stuck."""
    osname = env("VM_OS_NAME") or "vm"
    monport = read_state(osname, "monport")
    serport = read_state(osname, "serport")
    vncport = read_state(osname, "vncport") or "5900"
    log("waiting for %s to power off (poll %ds; vnc 127.0.0.1::%s, "
        "monitor 127.0.0.1:%s, serial 127.0.0.1:%s -> %s.serial.log)"
        % (what, poll, vncport, monport, serport, osname))
    elapsed = 0
    while isRunning() == 0:
        time.sleep(poll)
        elapsed += poll
        size, tail = _serial_tail_line()
        mm, ss = divmod(elapsed, 60)
        log("[%dm%02ds] %s, serial=%dB | %s" % (mm, ss, what, size, tail[:140]))
    log("%s powered off after %d s" % (what, elapsed))


def clearVM():
    osname = _check_osname("clearVM")
    if not osname: return 1
    if isRunning() == 0:
        destroyVM()
    closeConsole()
    for f in ["%s.qcow2" % osname, "%s.img" % osname, "%s.pid" % osname,
              "%s.monport" % osname, "%s.serport" % osname, "%s.sshport" % osname,
              "%s.vncport" % osname,
              "%s.serial.log" % osname, "%s.qemu.log" % osname, "%s.cmdline" % osname,
              "%s-QEMU_EFI.fd" % osname, "%s-QEMU_EFI_VARS.fd" % osname]:
        try: os.remove(f)
        except OSError: pass
    try: os.remove(os.path.join(HOME, ".ssh", "known_hosts"))
    except OSError: pass
    return 0


# ============================================================================
# (H) OCR + screenText + waitForText + startWeb
# ============================================================================

def ocr_tess(img):
    try:
        return subprocess.run(["tesseract", "-l", "eng", img, "-"],
                             capture_output=True, text=True).stdout
    except Exception:
        return ""


def ocr_py(img):
    try:
        import cv2, numpy, pytesseract
    except ImportError:
        return ocr_tess(img)
    im = cv2.imread(img)
    gray = cv2.cvtColor(im, cv2.COLOR_RGB2GRAY)
    _, img_bin = cv2.threshold(gray, 128, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    gray = cv2.bitwise_not(img_bin)
    kernel = numpy.ones((2, 1), numpy.uint8)
    im2 = cv2.erode(gray, kernel, iterations=1)
    im2 = cv2.dilate(im2, kernel, iterations=1)
    return pytesseract.image_to_string(im2)


def ocr(img):
    if env("VM_OCR") == "py":
        return ocr_py(img)
    return ocr_tess(img)


def vnc_capture(pngpath):
    while True:
        rc = subprocess.run(["vncdotool"] + vnc_server() + ["capture", pngpath],
                           stdout=DEVNULL, stderr=DEVNULL).returncode
        if rc == 0: return
        time.sleep(3)


# Public VNC helpers usable from hooks. These are thin wrappers over the
# vncdotool CLI so hook code never needs to subprocess directly. They are
# VNC-mode-only -- in console-build mode the keyboard helpers (string/enter/
# tab/...) talk to the serial socket and a serial console has no mouse, no
# super-alt-t, etc. Calling these in console mode will still try to drive
# vncdotool but typically have no effect on the guest.

def vncKey(key):
    """Send a key event over VNC. `key` is a vncdotool name like 'enter',
    'right', 'tab', 'super-alt-t', 'ctrl-c'."""
    return subprocess.run(["vncdotool"] + vnc_server() + ["key", str(key)]).returncode


def vncMove(x, y):
    """Move the VNC pointer to absolute (x, y)."""
    return subprocess.run(["vncdotool"] + vnc_server() + ["move", str(x), str(y)]).returncode


def vncClick(button=1):
    """Click a VNC mouse button (1=left, 2=middle, 3=right)."""
    return subprocess.run(["vncdotool"] + vnc_server() + ["click", str(button)]).returncode


def vncMoveClick(x, y, button=1):
    """Move the pointer to (x, y) and click `button`, in one vncdotool call
    (single TCP round trip to the VNC server)."""
    return subprocess.run(["vncdotool"] + vnc_server() + ["move", str(x), str(y),
                           "click", str(button)]).returncode


def vncType(text):
    """Type a literal string over VNC (with --force-caps for layout safety)."""
    return subprocess.run(["vncdotool"] + vnc_server() + ["--force-caps", "type", text]).returncode


def _write_index_html(text):
    head = ("<!DOCTYPE html>\n<html>\n<head>\n<title>%s %s</title>\n"
            "<meta http-equiv='refresh' content='1'>\n</head>\n"
            "<body onclick='stop()' style='background-color:grey;'>\n\n"
            "<img src='screen.png' alt='Screen'>\n\n<br>\n<pre>\n"
            % (env("VM_OS_NAME") or "", env("VM_RELEASE")))
    with open("index.html", "w") as f:
        f.write(head); f.write(text); f.write("</pre></body></html>\n")


def _screen_text_value(img=None):
    osname = env("VM_OS_NAME") or "vm"
    if env("VM_USE_CONSOLE_BUILD"):
        text = tail_file(serial_log(osname), 50)
    else:
        png = img if img else tempfile.mktemp(suffix=".png")
        vnc_capture(png)
        try: os.chmod(png, 0o666)
        except OSError: pass
        text = ocr(png)
        if not img:
            try: os.remove(png)
            except OSError: pass
    if img:
        with open("screen.txt", "w") as f:
            f.write(text)
        _write_index_html(text)
    return text


def screenText(img=None):
    if not _check_osname("screenText"): return 1
    text = _screen_text_value(img)
    if not img:
        sys.stdout.write(text)
    return 0


def screenTextValue():
    """Return the current OCR'd VNC screen (or tail of the serial log in
    console-build mode) as a string. For hooks doing text matching, e.g.
        while "Welcome to ..." not in screenTextValue(): vncKey("super-alt-t")
    osname comes from VM_OS_NAME just like all the other hook-facing helpers."""
    if not _check_osname("screenTextValue"): return ""
    return _screen_text_value()


def waitForText(text=None, sec="", hook=None):
    """Poll the screen (VNC OCR or serial-console capture) every 3 s until
    `text` is found in it, or `sec` *wall-clock seconds* elapse. If `hook` is
    given, call it on every poll BEFORE the screen capture -- useful for
    re-asserting an action that may have been swallowed across a guest state
    change (e.g. sending Ctrl+Alt+F2 every poll until the text console getty
    appears). `hook` may be a Python callable (preferred) or a shell command
    string (run via `bash -c ...`, kept for porting old hooks).

    Both code paths -- match and timeout -- return 0, so the caller's
    processOpts unconditionally fires the keystrokes regardless of outcome
    (intentional: if a screen we expected never showed up, pressing the keys
    anyway often advances the installer to the next screen we DO recognise).
    """
    if not text:
        log("Usage: waitForText text [sec]"); return 1
    if not _check_osname("waitForText"): return 1
    sec = (str(sec) or "").strip()
    log("Waiting for text: %s" % text)
    deadline = time.time() + int(sec) if sec else None
    while (deadline is None) or (time.time() < deadline):
        if hook is not None:
            try:
                if callable(hook):
                    hook()
                else:
                    subprocess.run(["bash", "-c", str(hook)])
            except Exception as e:
                log("waitForText hook raised: %s" % e)
        time.sleep(3)
        screen = _screen_text_value(None)
        with open("_screenText.txt", "w") as f:
            f.write(screen)
        log(""); log("==========screen Text============")
        log(screen); log("==========screen Text end============")
        if text in screen:
            log("====> OK, found: %s" % text); return 0
        elif env("DEBUG"):
            log("Not found for text: %s" % text)
    log("Timeout for text: %s" % text)
    return 0


_startweb_thread = None
_startweb_stop = threading.Event()


def startWeb(needOCR=None):
    """Start the local HTTP server + a background screen-capture loop in a
    daemon thread (was a detached subprocess)."""
    osname = _check_osname("startWeb")
    if not osname: return 1
    try: os.remove("_stopvnc.txt")
    except OSError: pass
    # The HTTP server can stay as a detached subprocess: it shares cwd with us
    # and only serves whatever screenshots / OCR text we drop into the dir.
    subprocess.Popen([sys.executable, "-m", "http.server"],
                    stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                    start_new_session=True)
    if not os.path.exists("index.html"):
        with open("index.html", "w") as f:
            f.write("<!DOCTYPE html>\n<html>\n<head>\n<title>%s</title>\n"
                    "<meta http-equiv='refresh' content='1'>\n</head>\n"
                    "<body style='background-color:grey;'>\n\n"
                    "<h1>Please just wait....<h1>\n\n</body>\n</html>\n" % osname)

    def loop():
        while not _startweb_stop.is_set():
            if not os.path.exists("_stopvnc.txt"):
                try:
                    _screen_text_value("screen.png")
                except Exception:
                    pass
            time.sleep(3)
    global _startweb_thread
    _startweb_thread = threading.Thread(target=loop, daemon=True, name="startweb-loop")
    _startweb_thread.start()
    return 0


def pauseVNC():
    open("_stopvnc.txt", "w").close()
    return 0


# ============================================================================
# (I) SSH / IP / export
# ============================================================================

def getVMIP():
    """Returns the guest's slirp IP from the QEMU monitor; for *logging only*
    under slirp -- the host has no route to the guest's 192.168.122.x. Host->
    guest SSH MUST go through the hostfwd port on 127.0.0.1."""
    return parse_usernet_ip(qmon("info usernet") or "") or ""


def addSSHHost(idfile=None, user=None):
    osname = _check_osname("addSSHHost")
    if not osname: return 1
    if not user:
        user = "user" if osname == "haiku" else "root"
    idrsa = os.path.join(HOME, ".ssh", "id_rsa")
    if not os.path.exists(idrsa):
        run(["ssh-keygen", "-f", idrsa, "-q", "-N", ""])
    sshport = read_state(osname, "sshport") or "22"
    sshdir = os.path.join(HOME, ".ssh")
    os.makedirs(sshdir, exist_ok=True)
    with open(os.path.join(sshdir, "config"), "a") as f:
        f.write("\nInclude config.d/*\nStrictHostKeyChecking=accept-new\n"
                "SendEnv   CI  GITHUB_*\n\n")
    os.makedirs(os.path.join(sshdir, "config.d"), exist_ok=True)
    conf = ("\nHost %s\n  User %s\n  HostName 127.0.0.1\n  Port %s\n"
            "  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n"
            % (osname, user, sshport))
    if idfile:
        conf += "  IdentityFile=%s\n" % idfile
    with open(os.path.join(sshdir, "config.d", "%s.conf" % osname), "w") as f:
        f.write(conf)
    localbin = os.path.join(HOME, ".local", "bin")
    os.makedirs(localbin, exist_ok=True)
    launcher = os.path.join(localbin, osname)
    with open(launcher, "w") as f:
        f.write("#!/usr/bin/env sh\n\nssh %s sh<$1\n" % osname)
    os.chmod(launcher, 0o755)
    return 0


def addSSHAuthorizedKeys(pbk=None):
    if not pbk:
        log("Usage: addSSHAuthorizedKeys id_rsa.pub"); return 1
    ak = os.path.join(HOME, ".ssh", "authorized_keys")
    os.makedirs(os.path.dirname(ak), exist_ok=True)
    with open(pbk) as src, open(ak, "a") as dst:
        dst.write(src.read())
    os.chmod(ak, 0o600)
    return 0


def addNAT(proto=None, hostPort=None, vmPort=None):
    if not _check_osname("addNAT"): return 1
    if not vmPort:
        log("Usage: addNAT protocol hostPort vmPort"); return 1
    if qmon("hostfwd_add %s:127.0.0.1:%s-:%s" % (proto, hostPort, vmPort)) is None:
        log("addNAT: monitor not available"); return 1
    return 0


def exportOVA(ova=None, xml=None):
    osname = _check_osname("exportOVA")
    if not osname: return 1
    if not ova:
        log("Usage: exportOVA out.qcow2 [out.xml]"); return 1
    src = "%s.qcow2" % osname
    log(src)
    run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off", src, ova])
    sh("zstd -c %s | split -b 2000M -d -a 1 - %s"
       % (shlex.quote(ova), shlex.quote(ova + ".zst.")))
    run(["ls", "-lah"])
    try: os.rename(ova + ".zst.0", ova + ".zst")
    except OSError: pass
    sh("chmod +r %s* 2>/dev/null || true" % shlex.quote(ova + ".zst"))
    if xml:
        cl = state(osname, "cmdline")
        if os.path.exists(cl):
            shutil.copy(cl, xml)
        else:
            with open(xml, "w") as f:
                f.write("# no launch descriptor recorded for %s\n" % osname)
    return 0


# ============================================================================
# (J) Key / text injection
# ============================================================================

def _key(console_seq, vnc_key):
    if env("VM_USE_CONSOLE_BUILD"):
        _send_console(console_seq)
    else:
        run(["vncdotool"] + vnc_server() + ["key", vnc_key])


def string(*args):
    """Inject a literal string into the guest console. VM_OS_NAME must be
    set; the build pipeline sets it from the conf, and exec()'d hooks
    inherit it via this module's globals. The parts are joined with a
    single space.

      string("dhclient vtnet0")  -> guest types `dhclient vtnet0`
      string("a", "b")           -> guest types `a b`

    Do NOT pass osname as a leading arg. The old API accepted it and that
    accidentally produced `# midnightbsd dhclient vtnet0` (root cause of the
    initial MidnightBSD runs hanging at /bin/sh: midnightbsd: not found)."""
    if not env("VM_OS_NAME"):
        log("string: VM_OS_NAME not set"); return 1
    text = " ".join(args)
    if env("VM_USE_CONSOLE_BUILD"):
        _send_console(text)
    else:
        # --delay spaces out the synthetic keypresses (ms between events).
        # Without it, vncdotool fires the whole string as fast as it can;
        # under raw QEMU's VNC (faster than the old libvirt path) a slow
        # framebuffer console -- e.g. OpenBSD bsd.rd's wscons -- drops the
        # tail of a long string. That silently truncated the autoinstall
        # response-file URL ("http://192.168.122.1:8000/conf/...resp" ->
        # "http://192.168.122.1"), so the installer never fetched the resp
        # and hung in interactive mode. The old vbox.sh already used
        # --delay=150 for typefile; short strings only have a few chars so
        # the added latency is negligible, long ones (URLs) now arrive intact.
        run(["vncdotool"] + vnc_server() + ["--force-caps", "--delay=40", "type", text])
    return 0


def _check_osname(funcname):
    o = env("VM_OS_NAME")
    if not o:
        log("%s: VM_OS_NAME not set" % funcname)
    return o


def space():
    if not _check_osname("space"): return 1
    if env("VM_USE_CONSOLE_BUILD"):
        _send_console(" ")
    else:
        run(["vncdotool"] + vnc_server() + ["type", " "])
    return 0


def enter():
    if not _check_osname("enter"): return 1
    _key("\r", "enter"); return 0


def tab():
    if not _check_osname("tab"): return 1
    _key("\t", "tab"); return 0


def f2():
    if not _check_osname("f2"): return 1
    _key("\x1b[12~", "f2"); return 0


def f7():
    if not _check_osname("f7"): return 1
    _key("\x1b[18~", "f7"); return 0


def f8():
    if not _check_osname("f8"): return 1
    _key("\x1b[19~", "f8"); return 0


def down():
    if not _check_osname("down"): return 1
    _key("\x1b[B", "down"); return 0


def up():
    if not _check_osname("up"): return 1
    _key("\x1b[A", "up"); return 0


def ctrlD():
    if not _check_osname("ctrlD"): return 1
    _key("\x04", "ctrl-d"); return 0


KEYFUNCS = {
    "enter": enter, "space": space, "tab": tab, "f2": f2, "f7": f7, "f8": f8,
    "down": down, "up": up, "ctrlD": ctrlD,
}


def _dispatch_keygroup(group):
    if not group: return
    cmd, rest = group[0], group[1:]
    if cmd == "string":
        # rest tokens come from shlex with posix quoting; rejoin for type/send.
        string(*rest)
    elif cmd == "sleep":
        try: time.sleep(float(rest[0]))
        except (ValueError, IndexError): pass
    elif cmd in KEYFUNCS:
        KEYFUNCS[cmd]()
    else:
        # Fall through to a PATH command. Mirrors the old bash `inputKeys` /
        # `input osname "..."` semantics, which did `eval "$*"` and would run
        # any shell command (e.g. `vncdotool key super-alt-t` directly inside
        # an opts.txt step). We run it as argv (no shell interpretation), so
        # quoting / metachars don't sneak in.
        try:
            subprocess.run([cmd] + list(rest))
        except FileNotFoundError:
            log("input: unknown key command and not on PATH: %s" % cmd)
        except Exception as e:
            log("input: %s: %s" % (cmd, e))


def _run_keyseq(keystr):
    """Tokenize keystr respecting quotes with ';' as a separate token, then
    split into groups -- replaces bash `eval "$*"` without shell."""
    try:
        lex = shlex.shlex(keystr, posix=True, punctuation_chars=";")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        for grp in keystr.split(";"):
            _dispatch_keygroup(grp.split())
        return
    group, groups = [], []
    for tk in tokens:
        if tk == ";":
            groups.append(group); group = []
        else:
            group.append(tk)
    groups.append(group)
    for grp in groups:
        _dispatch_keygroup(grp)


def input_cmd(*keyparts):
    """Original bash function 'input osname "string xxx; enter"'. osname is
    taken from VM_OS_NAME env (set by the build pipeline)."""
    if not _check_osname("input"): return 1
    _run_keyseq(" ".join(keyparts))
    return 0


# ============================================================================
# (K) File feeders (inputFile* / uploadFile)
# ============================================================================

def _serve_file_nc(fpath, port):
    """Spawn nc detached so it outlives this function call -- the guest will
    connect to it later."""
    f = open(fpath, "rb")
    subprocess.Popen(["nc", "-q", "0", "-l", str(port)], stdin=f,
                    stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
    f.close()


def inputFile(fpath=None):
    if not _check_osname("inputFile"): return 1
    if not fpath:
        log("Usage: inputFile file.txt"); return 1
    if env("VM_USE_CONSOLE_BUILD"):
        _serve_file_nc(fpath, 64342)
        string("nc  192.168.122.1 64342 | sh")
        enter()
    else:
        run(["vncdotool"] + vnc_server() + ["--force-caps", "--delay=150", "typefile", fpath])
    return 0


def inputFileNC(fpath=None):
    if not _check_osname("inputFileNC"): return 1
    if not fpath:
        log("Usage: inputFileNC file.txt"); return 1
    _serve_file_nc(fpath, 64342)
    string("nc  192.168.122.1 64342 | sh")
    enter()
    return 0


def inputFileTelnet(fpath=None):
    if not _check_osname("inputFileTelnet"): return 1
    if not fpath:
        log("Usage: inputFileTelnet file.txt"); return 1
    _serve_file_nc(fpath, 64342)
    string("( sleep 1; ) | telnet 192.168.122.1 64342 | bash")
    enter()
    return 0


def inputFileBash(fpath=None):
    if not _check_osname("inputFileBash"): return 1
    if not fpath:
        log("Usage: inputFileBash file.txt"); return 1
    _serve_file_nc(fpath, 64342)
    string("bash -c 'bash <(exec 3<>/dev/tcp/192.168.122.1/64342; cat <&3)'")
    enter()
    return 0


def inputFileStdIn(fpath=None):
    if not _check_osname("inputFileStdIn"): return 1
    if not fpath:
        log("Usage: inputFileStdIn file.txt"); return 1
    with open(fpath, errors="replace") as f:
        for line in f:
            string(line.rstrip("\n"))
            enter()
            time.sleep(1)
    return 0


def uploadFile(local=None, remote=None):
    if not _check_osname("uploadFile"): return 1
    if not remote:
        log("Usage: uploadFile local remote"); return 1
    if env("VM_USE_CONSOLE_BUILD"):
        _serve_file_nc(local, 64343)
        string("nc  192.168.122.1 64343 >%s" % remote)
        enter()
    else:
        string("cat - >%s" % remote)
        enter()
        inputFile(local)
        ctrlD()
    return 0


def processOpts(optsfile=None):
    if not _check_osname("processOpts"): return 1
    if not optsfile:
        log("Usage: processOpts optsfile"); return 1
    with open(optsfile, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.replace("#", "").replace(" ", ""):
                continue
            if line.lstrip().startswith("#"):
                continue
            log("====> %s" % line)
            parts = line.split("|")
            text = parts[0].strip() if len(parts) > 0 else ""
            keys = parts[1] if len(parts) > 1 else ""
            timeout = parts[2].strip() if len(parts) > 2 else ""
            log("========> Text:    %s" % text)
            log("========> Keys:    %s" % keys)
            log("========> Timeout: %s" % timeout)
            if waitForText(text, timeout) == 0:
                log("Input keys: %s" % keys)
                input_cmd(keys)
            else:
                log("Timeout for waiting for text: %s" % text)
            time.sleep(1)
    return 0


# ============================================================================
# (L) Hook runner + conf loader
# ============================================================================

def run_hook(name):
    """Run a hook. Returns True if any hook ran. Where the hook runs is encoded
    in the filename prefix:

      hooks/host_<name>.py  -- host-side, exec()'d into THIS module's globals.
                               The hook can call build.py functions directly
                               (waitForText, inputKeys, string, enter,
                               screenText, ...) and see pipeline globals
                               (osname, ostype, sshport, opts) as bare names.
                               Use whenever the hook needs the VM-abstraction
                               API. Lookup precedence #1.

      hooks/host_<name>.sh  -- host-side, plain `bash` subprocess. The conf's
                               VM_* env vars are inherited. Use for straight
                               bash tooling on the host that does NOT need
                               build.py functions (virt-customize, qemu-img,
                               shell glue, ...). Lookup precedence #2.

      hooks/vm_<name>.sh    -- guest-side, piped into the guest's sh via SSH
                               with SendEnv=VM_RELEASE. Use for in-guest
                               configuration (service xxx enable, sysrc,
                               editing /etc/*, installing packages, ...).
                               Guest hooks are always .sh because the guest
                               is not guaranteed to have Python. Lookup
                               precedence #3.

    Callers pass the logical hook name (e.g. "installOpts", "postBuild");
    the prefix lookup is internal."""
    py = "hooks/host_%s.py" % name
    if os.path.exists(py):
        log(py)
        with open(py) as f:
            code = f.read()
        log(code)
        g = globals()
        g.setdefault("__hookname__", name)
        exec(compile(code, py, "exec"), g)
        return True
    host_sh = "hooks/host_%s.sh" % name
    if os.path.exists(host_sh):
        log(host_sh)
        with open(host_sh) as f:
            log(f.read())
        subprocess.run(["bash", host_sh], env=os.environ.copy())
        return True
    vm_sh = "hooks/vm_%s.sh" % name
    if os.path.exists(vm_sh):
        log(vm_sh)
        with open(vm_sh) as f:
            log(f.read())
        with open(vm_sh, "rb") as f:
            subprocess.run(
                ["ssh", "-o", "SendEnv=VM_RELEASE",
                 globals().get("osname") or env("VM_OS_NAME"), "sh"],
                stdin=f)
        return True
    return False


def inputKeys(keys):
    """Convenience alias for input_cmd(keys). osname is taken from VM_OS_NAME."""
    return input_cmd(keys)


def conf_load(path):
    """Source a bash-style conf via `bash -c '. file; env'` and import VM_*,
    SEC_* into our environment. Handles bash variable interpolation cleanly,
    so we don't have to write a bash KEY=VALUE parser."""
    if not os.path.exists(path):
        log("conf not found: %s" % path); return False
    # `set -a` auto-exports every variable assigned while the conf is sourced,
    # so plain `VM_FOO="bar"` lines show up in `env` (the conf doesn't have to
    # write `export VM_FOO=...` explicitly).
    out = subprocess.check_output(
        ["bash", "-c", "set -a; . %s 2>/dev/null; set +a; env" % shlex.quote(path)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    for line in out.decode("utf-8", "replace").splitlines():
        if "=" not in line: continue
        k, v = line.split("=", 1)
        if k.startswith(("VM_", "SEC_")):
            os.environ[k] = v
    return True


# ============================================================================
# (M) Build pipeline (was build.sh)
# ============================================================================

def _ssh_ready_check(timeout=10):
    """Return True if `ssh $VM_OS_NAME exit` succeeds within `timeout` seconds.
    Uses subprocess.run(timeout=...) instead of the external `timeout` binary;
    on TimeoutExpired the child is killed and we return False.

    Default 10 s -- short enough that retry polling stays snappy, long enough
    to cover SSH banner + key exchange + auth on slow guests (illumos sshd
    in particular takes 4-5 s for the full handshake even on a healthy KVM
    boot; an over-tight 2 s window misreads a working sshd as down)."""
    osname = env("VM_OS_NAME")
    if not osname: return False
    cmd = ["ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR",
           "-o", "ConnectTimeout=%d" % max(1, int(timeout)),
           osname, "exit"]
    try:
        return subprocess.run(cmd, stdout=DEVNULL, stderr=DEVNULL,
                              timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _wait_ssh(max_retries=100, restart_cb=None):
    """Poll ssh through the hostfwd port until reachable; optional restart_cb
    runs once on terminal failure.

    Every retry we ALSO run the layered IP probe (anyvm.py:6100-6118
    "hostfwd guard"): under slirp the guest's DHCP lease should be
    192.168.122.10 (matching the hostfwd we wired at launch), but stale
    leases / DHCP pickup races can land it on .11 / .12 / ... in which case
    the host-side ssh through hostfwd hits a dead IP forever. When we
    detect a different actual IP we rewrite the hostfwd via the monitor on
    the fly so the very next ssh probe lands on the right guest.

    Detection cascade (mirrors anyvm.py:6109):
      1. `info usernet` -- slirp's outbound flow table
      2. serial.log    -- dhclient's "bound to <ip>" / rc.d's "inet <ip>"
    """
    osname = env("VM_OS_NAME") or ""
    sshport_str = read_state(osname, "sshport") or "22"
    try:
        sshport = int(sshport_str)
    except ValueError:
        sshport = 22
    retry = 0
    restarted = False
    guard_done = False
    while not _ssh_ready_check(10):
        log("ssh is not ready, just wait.")
        if not guard_done:
            actual = detect_guest_ip(osname)
            if actual:
                if actual == SLIRP_EXPECTED_GUEST_IP:
                    log("guest IP %s matches hostfwd target, ok" % actual)
                    guard_done = True
                else:
                    log("guest IP %s != expected %s; rewriting hostfwd via monitor"
                        % (actual, SLIRP_EXPECTED_GUEST_IP))
                    if rewrite_hostfwd_target(sshport, actual):
                        log("hostfwd rewritten to %s:22" % actual)
                        guard_done = True
                    else:
                        log("hostfwd rewrite failed; will retry next iteration")
        time.sleep(10)
        retry += 1
        if retry > max_retries:
            if restarted or not restart_cb:
                log("ssh is failed."); return False
            log("ssh failed; trying restart")
            restarted = True
            restart_cb()
            retry = 0
            guard_done = False  # new boot, recheck IP
    return True


def start_and_wait():
    osname = _check_osname("start_and_wait")
    if not osname: return
    startVM(); time.sleep(2); openConsole()
    if not run_hook("waitForLoginTag"):
        waitForText(env("VM_LOGIN_TAG"))
    time.sleep(3)


def shutdown_and_wait():
    osname = _check_osname("shutdown_and_wait")
    if not osname: return
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
           osname, env("VM_SHUTDOWN_CMD")]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        log("shutdown rc=%d, ignoring (haiku?)" % rc)
    time.sleep(30)
    if isRunning() == 0:
        if shutdownVM() != 0:
            log("shutdown error")
    _wait_vm_down(what="VM shutdown", poll=5)
    closeConsole()


def restart_and_wait():
    shutdown_and_wait(); start_and_wait()


def _prep_vhd_disk(link):
    """Materialize $osname.qcow2 from a published cloud image URL."""
    osname = env("VM_OS_NAME")
    qcow = "%s.qcow2" % osname
    if os.path.exists(qcow): return
    if link.endswith("img.gz"):
        img = "%s.img" % osname
        if not os.path.exists(img):
            try: os.remove(img + ".gz")
            except OSError: pass
            download(link, img + ".gz")
            sh("gunzip -c %s.gz > %s" % (shlex.quote(img), shlex.quote(img)))
        run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
             "-o", "preallocation=off", img, qcow])
    elif link.endswith("img.zst"):
        img = "%s.img" % osname
        if not os.path.exists(img):
            try: os.remove(img + ".zst")
            except OSError: pass
            download(link, img + ".zst")
            run(["zstd", "-f", "-d", img + ".zst", "-o", img])
        run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
             "-o", "preallocation=off", img, qcow])
    elif link.endswith(".img"):
        tmp = "%s.download.img" % osname
        if not os.path.exists(tmp):
            download(link, tmp)
        run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off", tmp, qcow])
        try: os.remove(tmp)
        except OSError: pass
    elif link.endswith(".qcow2"):
        tmp = "%s.download.qcow2" % osname
        if not os.path.exists(tmp):
            download(link, tmp)
        run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off", tmp, qcow])
        try: os.remove(tmp)
        except OSError: pass
    else:
        xz = qcow + ".xz"
        if not os.path.exists(xz):
            download(link, xz)
        run(["xz", "-d", "-T", "0", "--verbose", xz])


def _gen_enablessh_local():
    """Build enablessh.local: enablessh.txt + authorized_keys append (twice,
    once base64-roundtripped to dodge encoding bugs we've seen in console
    paste paths) + chmod."""
    idrsa = os.path.join(HOME, ".ssh", "id_rsa")
    if not os.path.exists(idrsa):
        run(["ssh-keygen", "-f", idrsa, "-q", "-N", ""])
    pub_path = idrsa + ".pub"
    pub = open(pub_path).read().rstrip("\n")

    try: os.remove("enablessh.local")
    except OSError: pass
    shutil.copy("enablessh.txt", "enablessh.local")
    with open("enablessh.local", "a") as f:
        f.write("echo '%s' >>~/.ssh/authorized_keys\n\n\n\n" % pub)
        b64 = base64.b64encode(pub.encode("utf-8")).decode("ascii")
        f.write("echo '%s' | openssl base64 -d >>~/.ssh/authorized_keys\n\n\n"
                % b64)
        f.write("\nchmod 600 ~/.ssh/authorized_keys\n\n\n")
    log(open("enablessh.local").read())


def _enable_ssh_root_branch(sshport):
    """The VM_USE_SSHROOT_BUILD_SSH path: sshpass into root@guest, feed
    enablessh.local; under slirp we connect via the hostfwd port on 127.0.0.1
    (the guest's 192.168.122.x is not host-reachable)."""
    vmip = getVMIP()
    log("guest slirp ip: %s (connecting via hostfwd 127.0.0.1:%s)" % (vmip, sshport))
    with open("enablessh.local", "rb") as inp:
        subprocess.run(
            ["sshpass", "-p", env("VM_ROOT_PASSWORD"), "ssh", "-p", str(sshport),
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-tt",
             "root@127.0.0.1", "TERM=xterm"],
            stdin=inp)
    time.sleep(10)
    inputKeys("enter"); time.sleep(2)
    inputKeys("enter"); time.sleep(2)
    log("check ssh access:")
    subprocess.call(
        ["ssh", "-p", str(sshport), "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-vv", "root@127.0.0.1", "pwd"])
    log("ssh OK")


def _enable_ssh_console_branch():
    """The console-paste path used when there's no sshd reachable yet."""
    log("login as root at console.")
    inputKeys("enter"); inputKeys("enter"); time.sleep(20)
    inputKeys("enter"); inputKeys("enter")
    inputKeys("string root; enter; sleep 5;")
    if env("VM_ROOT_PASSWORD"):
        inputKeys("string %s ; enter" % env("VM_ROOT_PASSWORD"))
        time.sleep(10)
    inputKeys("enter"); time.sleep(20); inputKeys("enter")
    screenText()
    if run_hook("enableNetwork"):
        screenText(); time.sleep(60)
    if env("VM_USE_NC_ENABLE_SSH"):
        inputFileNC("enablessh.local")
    elif env("VM_USE_BASH_ENABLE_SSH"):
        inputFileBash("enablessh.local")
    else:
        inputFile("enablessh.local")
    time.sleep(60); screenText(); time.sleep(10)
    inputKeys("enter"); time.sleep(2)
    inputKeys("enter"); time.sleep(2)


def _send_env_check():
    """sanity-check that ssh SendEnv passes GITHUB_ANYVM through."""
    osname = env("VM_OS_NAME")
    p = subprocess.run(
        ["ssh", osname, "sh", "-c", "env"], capture_output=True,
        env={**os.environ, "GITHUB_ANYVM": "1"})
    if b"GITHUB_" in p.stdout:
        log("SendEnv OK"); return True
    log("SendEnv is not working")
    log("===============env====")
    sh("env")
    log("=============ssh env==")
    subprocess.call(["ssh", osname, "sh", "-c", "env"])
    log("=========check data===")
    sh("pwd; ls -lah .; ls -lah ~; ls -lah ~/.ssh")
    if os.path.exists(os.path.expanduser("~/.ssh/config")):
        sh("cat ~/.ssh/config")
    if os.path.exists(os.path.expanduser("~/.ssh/config.d")):
        sh("cat ~/.ssh/config.d/*")
    log("====== check data in vm====")
    subprocess.call(["ssh", osname, "ls -lah"])
    subprocess.call(["ssh", osname, "ls -lah .ssh"])
    subprocess.call(["ssh", osname, "cat .ssh/*"])
    subprocess.call(["ssh", osname, "cat /etc/ssh/sshd_config"])
    return False


def main(argv):
    if len(argv) < 2:
        log("Please give the conf file")
        return 1
    conf_path = argv[1]
    if not conf_load(conf_path):
        return 1

    # Expose pipeline globals to hooks (so hook code can use bare `osname`
    # etc., mirroring how build.sh's source-d hooks saw shell variables).
    g = globals()
    g["osname"] = env("VM_OS_NAME")
    g["ostype"] = env("VM_OS_TYPE")
    g["sshport"] = env("VM_SSH_PORT")
    g["opts"] = env("VM_OPTS")
    osname = g["osname"]
    ostype = g["ostype"]
    sshport = g["sshport"]
    opts = g["opts"]

    startWeb("needOCR")
    setup("needOCR")

    log("============== host CPU ==============")
    sh("lscpu || cat /proc/cpuinfo || true")
    log("=====================================")

    if clearVM() != 0:
        log("vm does not exist (ok)")

    if env("VM_ISO_LINK"):
        createVM(env("VM_ISO_LINK"), ostype, sshport, env("VM_PRE_DISK_LINK"))
        time.sleep(2)
        openConsole()
        if not run_hook("installOpts"):
            processOpts(opts)
            log("sleep 60 seconds. just wait")
            time.sleep(60)
            if isRunning() == 0:
                if shutdownVM() != 0:
                    log("shutdown error")
                if destroyVM() != 0:
                    log("destroyVM error")
        _wait_vm_down(what="install", poll=20)
        closeConsole()
        # No CDROM detach needed: the next startVM relaunches QEMU without any
        # install media, so the installed system boots from the disk directly.
    elif env("VM_VHD_LINK"):
        _prep_vhd_disk(env("VM_VHD_LINK"))
        run_hook("prepareImage")
        createVMFromVHD(ostype, sshport)
        time.sleep(5)
    else:
        log("no VM_ISO_LINK or VM_VHD_LINK, can not build.")
        return 1

    log("VM image size immediately after install:")
    sh("ls -lh")

    if not env("VM_NO_VNC_BUILD"):
        os.environ["VM_USE_CONSOLE_BUILD"] = ""

    start_and_wait()
    _gen_enablessh_local()

    if not run_hook("enablessh"):
        if env("VM_USE_SSHROOT_BUILD_SSH"):
            _enable_ssh_root_branch(sshport)
        else:
            _enable_ssh_console_branch()

    addSSHHost()
    log("Sleep for the sshd to restart"); time.sleep(10)

    def _restart():
        if isRunning() == 0 and shutdownVM() != 0:
            log("shutdown error"); sys.exit(1)
        _wait_vm_down(what="VM restart", poll=5)
        closeConsole(); start_and_wait()

    if not _wait_ssh(restart_cb=_restart):
        return 1

    user = os.environ.get("USER", "user")
    ssh_init = (
        'echo "StrictHostKeyChecking=no" >.ssh/config\n'
        'echo "Host host" >>.ssh/config\n'
        'echo "     HostName  192.168.122.1" >>.ssh/config\n'
        'echo "     User %s" >>.ssh/config\n'
        'echo "     ServerAliveInterval 1" >>.ssh/config\n'
    ) % user
    subprocess.run(["ssh", osname, "sh"], input=ssh_init.encode())

    if run_hook("postBuild"):
        restart_and_wait()
        if not _wait_ssh():
            log("ssh is failed."); return 1

    output = "%s-%s" % (osname, env("VM_RELEASE"))
    if env("VM_ARCH"):
        output = "%s-%s" % (output, env("VM_ARCH"))
    with open("%s-id_rsa.pub" % output, "w") as f:
        subprocess.run(["ssh", osname, "cat ~/.ssh/id_rsa.pub"], stdout=f)

    if env("VM_PRE_INSTALL_PKGS"):
        cmd = "%s %s" % (env("VM_INSTALL_CMD"), env("VM_PRE_INSTALL_PKGS"))
        log(cmd)
        subprocess.run(["ssh", osname, "sh"], input=("set -e\n%s\n" % cmd).encode())

    run_hook("finalize")

    extra = env("VM_EXTRA_SCRIPT")
    if extra:
        log(extra)
        with open(extra, "rb") as f:
            subprocess.run(["ssh", "-o", "SendEnv=VM_RELEASE", osname, "sh"], stdin=f)

    shutdown_and_wait()

    # Host-side image-finalize hook (runs AFTER guest is down, BEFORE ISO is
    # removed below -- e.g. mounts the qcow2 to tweak files).
    run_hook("finalizeImage")

    if env("VM_ISO_LINK"):
        log("Clean up ISO for more space")
        try: os.remove("%s.iso" % osname)
        except OSError: pass

    log("contents of home directory:"); sh("ls -lah")
    log("free space:"); sh("df -h")

    ova = "%s.qcow2" % output
    xml = "%s.xml" % output
    log("Exporting %s" % ova)
    exportOVA(ova, xml)

    shutil.copy(os.path.join(HOME, ".ssh", "id_rsa"), "%s-host.id_rsa" % output)
    log("contents after export:"); sh("ls -lah")

    log("Checking the packages: %s %s" % (env("VM_RSYNC_PKG"), env("VM_SSHFS_PKG")))
    if not (env("VM_RSYNC_PKG") or env("VM_SSHFS_PKG")):
        log("skip")
    else:
        addSSHAuthorizedKeys("%s-id_rsa.pub" % output)
        startVM()
        while not _ssh_ready_check(timeout=10):
            log("not ready yet, just sleep."); time.sleep(5)
        if not _send_env_check():
            return 1
        if osname == "haiku":
            subprocess.call(["ssh", osname, "mkdir -p '$HOME/work'"])
            subprocess.call(["ssh", osname, "ls -lah '$HOME'"])
            log("======Show ssh config: ")
            subprocess.call(["ssh", osname, "cat /boot/system/settings/ssh/sshd_config"])
        else:
            subprocess.call(["ssh", osname, "mkdir -p $HOME/work"])
            subprocess.call(["ssh", osname, "ls -lah $HOME"])
            log("======Show ssh config: ")
            subprocess.call(["ssh", osname, "cat /etc/ssh/sshd_config"])

    log("Build finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
