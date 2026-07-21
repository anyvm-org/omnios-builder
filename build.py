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
import json
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


def must_run(cmd, what=None):
    """run() a command list but ABORT the build (clear FATAL + exit 1) on a
    non-zero exit, instead of silently continuing on a half-done/corrupt result.
    Use for irrecoverable pipeline steps (decompress, image convert, export)."""
    cp = subprocess.run(cmd)
    if cp.returncode != 0:
        log("FATAL: %s (exit %d): %s" % (
            what or "command failed", cp.returncode, " ".join(str(c) for c in cmd)))
        sys.exit(1)
    return cp


def must_sh(cmdstr, what=None):
    """sh() that ABORTS the build (FATAL + exit 1) on a non-zero exit."""
    rc = subprocess.call(cmdstr, shell=True)
    if rc != 0:
        log("FATAL: %s (exit %d): %s" % (what or "command failed", rc, cmdstr))
        sys.exit(1)
    return 0


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


def _run_loud(cmd, **kw):
    """Like _run_quiet, but echo the command and let whatever it prints stream
    straight to our stdout/stderr instead of capturing it. setup() uses this
    (via a local alias) so each host-dependency step is announced in the log
    as it starts -- a slow or hung install is then obvious from which command
    was echoed last, instead of the whole phase sitting silent."""
    log("setup: + " + " ".join(map(str, cmd)))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        log("FAILED (rc=%d): %s" % (r.returncode, " ".join(map(str, cmd))))
    return r


def _sh_loud(cmdstr):
    """Shell-string variant of _run_loud (output streams live)."""
    log("setup: + " + cmdstr)
    rc = subprocess.call(cmdstr, shell=True)
    if rc != 0:
        log("FAILED (rc=%d): %s" % (rc, cmdstr))
    return rc


# All build-GENERATED files -- state (pid/ports/cmdline), logs, the working
# qcow2, download intermediates, the web console (index.html/screen.png), and
# the final release artifacts (<output>.qcow2.zst + sidecars) -- live under
# WORKDIR so the builder repo root stays clean. INPUTS (conf/, hooks/, files/,
# .github/, the conf's VM_* paths) stay at the repo root and are read from
# there; they are never routed through wf(). The CI upload step
# (base-builder/.github/tpl/build.tpl.yml) picks the release artifacts up from
# this same WORKDIR -- keep the two in lock-step.
WORKDIR = "build"


def wf(name):
    """Path of a build-generated file, kept under WORKDIR."""
    return os.path.join(WORKDIR, name)


def state(osname, suffix):
    return wf("%s.%s" % (osname, suffix))


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
    if a == "s390x": return "qemu-system-s390x"
    # QEMU ships powerpc64 (big-endian) and powerpc64le (little-endian) under
    # the same binary; the -M pseries machine + -cpu pickselect the mode.
    if a in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        return "qemu-system-ppc64"
    if a in ("x86_64", "amd64"): return "qemu-system-x86_64"
    return "qemu-system-" + a


def resolve_qemu_bin():
    # VM_QEMU_BIN lets a conf swap in a different QEMU build (extracted by
    # setup() from a VM_QEMU_TAR tarball committed in the builder repo).
    # Needed when the distro QEMU is too old for a guest: Ubuntu 26.04
    # riscv64 needs QEMU >= 9.1 (see the riscv64 -cpu comment below).
    # QEMU locates its datadir relative to the binary (<bindir>/../share/
    # qemu), so an extracted bin/ + share/qemu tree works from any path.
    if env("VM_QEMU_BIN"):
        return os.path.abspath(env("VM_QEMU_BIN"))
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


def host_nested_amd_with_avx512():
    """True if the host is an AMD CPU that exposes AVX512 AND is itself
    running under a hypervisor (nested virtualization). Mirrors
    anyvm.py:host_nested_amd_with_avx512 (same rationale): nested AMD-V --
    e.g. KVM inside WSL2 / Hyper-V -- mishandles the L2 guest's AVX512
    XSAVE state, so a guest whose glibc selects the AVX512 string/memory
    routines randomly SIGSEGVs. The x86 KVM branch drops just avx512f from
    -cpu host in that exact case; bare-metal hosts have no 'hypervisor'
    flag and keep full AVX512."""
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/cpuinfo") as f:
            info = f.read()
    except Exception:
        return False
    return ("AuthenticAMD" in info
            and "hypervisor" in info
            and "avx512f" in info)


def qemu_accel():
    a = env("VM_ARCH") or "x86_64"
    if a in ("riscv64", "s390x"):
        return "tcg"
    if a == "aarch64":
        if HOST_ARCH in ("aarch64", "arm64"):
            if kvm_ok(): return "kvm"
            if hvf_supported(): return "hvf"
        return "tcg"
    if a in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        # KVM-HV / KVM-PR is only available when the host is also ppc64
        # (POWER8/POWER9 bare metal). On amd64 runners we always fall back
        # to TCG; pseries + power9 runs acceptably in TCG.
        if HOST_ARCH in ("ppc64", "ppc64le", "powerpc64", "powerpc64le"):
            if kvm_ok(): return "kvm"
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
      * s390x             -> virtio-net-ccw  (s390-ccw-virtio has a CCW bus,
                             not PCI; `virtio` in a conf maps to -ccw too)
    """
    arch = env("VM_ARCH") or "x86_64"
    n = env("VM_NIC")
    if n:
        if n in ("virtio", "virtio-net"):
            return "virtio-net-ccw" if arch == "s390x" else "virtio-net-pci"
        return n

    osname = env("VM_OS_NAME")
    release = env("VM_RELEASE")

    if arch == "s390x":
        return "virtio-net-ccw"
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
    """openbsd aarch64 install needs acpi=off (force FDT / device-tree mode).
    The libvirt-era builder that successfully installed every openbsd aarch64
    release (vbox.sh: virt-install ... --machine virt --noacpi) disabled ACPI
    for ALL aarch64, not just < 7.4: OpenBSD's bsd.rd installer kernel
    boot-loops under ACPI on the QEMU virt machine (kernel resets right after
    BOOTAA64 loads it -- reproduced on CI). anyvm.py only needs acpi=off for
    < 7.4 because it RUNS finished images (the installed kernel is fine under
    ACPI); the INSTALL path is what needs FDT. So: all openbsd aarch64."""
    return env("VM_OS_NAME") == "openbsd" and env("VM_ARCH") == "aarch64"


def make_blank(path, mb):
    # Write real zero bytes (NOT f.truncate(), which makes a sparse file).
    # Mirrors anyvm.py:create_sized_file. The aarch64 virt machine treats the
    # pflash images as a fixed 64MB raw flash device; a sparse-holed image can
    # make EDK2 misread the firmware and the guest resets in a boot loop (seen
    # on OpenBSD arm64). Fully-allocated zeros match anyvm.py's validated path.
    chunk = b"\0" * (1024 * 1024)
    with open(path, "wb") as f:
        for _ in range(mb):
            f.write(chunk)


def copy_into(src, dst):
    # Overwrite the start of dst without truncating it (like dd conv=notrunc).
    # Mirrors anyvm.py:copy_content_to_file.
    with open(src, "rb") as s: data = s.read()
    with open(dst, "r+b") as d: d.write(data)


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
# anyvm.py:5232-5238 lists the MERGED QEMU_EFI.fd first, and that is fine for
# its use case (RUNNING a finished image: the installed kernel never calls UEFI
# runtime services, so a CODE/VARS build mismatch is harmless). But we INSTALL
# from bsd.rd, whose installer kernel DOES call EFI runtime services -- with a
# merged CODE paired against AAVMF_VARS.fd (the only VARS the merged image can
# fall back to, from a different EDK2 build) the NVRAM/runtime layer desyncs and
# bsd.rd resets in a boot loop the moment it jumps to the kernel (reproduced on
# CI: bsd.rd loads, VM resets, forever). So prefer SPLIT firmware whose VARS
# companion sits in the same dir / same build (edk2-aarch64-code.fd ->
# edk2-aarch64-vars.fd, AAVMF_CODE.fd -> AAVMF_VARS.fd); keep merged images only
# as last-resort fallbacks.
_AARCH64_EFI_CODE_RELNAMES = [
    os.path.join("qemu", "edk2-aarch64-code.fd"),       # -> edk2-aarch64-vars.fd
    os.path.join("AAVMF", "AAVMF_CODE.fd"),             # -> AAVMF_VARS.fd
    os.path.join("edk2", "aarch64", "QEMU_EFI.fd"),     # merged (fallback)
    os.path.join("qemu-efi-aarch64", "QEMU_EFI.fd"),    # merged (fallback)
    os.path.join("edk2", "aarch64", "QEMU_EFI-pflash.raw"),
]


def _find_aarch64_efi_code(qemu_bin=None):
    """Locate the aarch64 EDK2 CODE firmware. Returns full path or ''.

    $VM_EFI_CODE overrides the search entirely -- per-conf knob for guests
    that need a non-default firmware (e.g. Ubuntu 26.04 aarch64 boots with
    AAVMF_CODE.secboot.fd + AAVMF_VARS.ms.fd because its shim is signed by
    Microsoft's Canonical sub-CA)."""
    override = env("VM_EFI_CODE")
    if override:
        if os.path.exists(override):
            return override
        log("VM_EFI_CODE=%s not found; falling back to autodetect" % override)
    for d in _aarch64_efi_search_dirs(qemu_bin):
        for rn in _AARCH64_EFI_CODE_RELNAMES:
            p = os.path.join(d, rn)
            if os.path.exists(p):
                return p
    return ""


def _find_aarch64_efi_vars(code_src, qemu_bin=None):
    """Find a VARS template matching the given CODE firmware.

    $VM_EFI_VARS overrides the search entirely -- per-conf knob for guests
    that need a non-default firmware (e.g. Ubuntu 26.04 aarch64 boots with
    AAVMF_CODE.secboot.fd + AAVMF_VARS.ms.fd because its shim is signed by
    Microsoft's Canonical sub-CA).

    Search CODE's own directory first (matching same-vendor pair) and then
    fall back to **all** EFI search dirs -- on Debian/Ubuntu the
    `qemu-efi-aarch64` package ships CODE as `/usr/share/qemu-efi-aarch64/
    QEMU_EFI.fd` but the only working VARS template is in a different
    directory `/usr/share/AAVMF/AAVMF_VARS.fd`; staying inside CODE's dir
    finds nothing and we fall back to a blank vars region, which crashes
    NetBSD evbarm + some other guests with SetVariable-NVRAM-init failures
    that present as a tight reboot loop at the [1.000xxx] kernel mark.

    A blank vars region is **always wrong on aarch64** -- if no template is
    found anywhere we return '' and the caller logs a warning.
    """
    override = env("VM_EFI_VARS")
    if override:
        if os.path.exists(override):
            return override
        log("VM_EFI_VARS=%s not found; falling back to autodetect" % override)
    if not code_src:
        return ""
    base = os.path.basename(code_src)

    def _name_guesses_in(d):
        gs = []
        # 1) Substituted-name pairs in the same directory (CODE/VARS, etc.)
        for a, b in [("QEMU_EFI", "QEMU_VARS"), ("_CODE", "_VARS"),
                     ("-code", "-vars"), ("CODE", "VARS")]:
            if a in base:
                gs.append(os.path.join(d, base.replace(a, b)))
        # 2) Common template basenames anywhere we look.
        gs += [
            os.path.join(d, "QEMU_VARS.fd"),
            os.path.join(d, "vars-template-pflash.raw"),
            os.path.join(d, "AAVMF_VARS.fd"),
        ]
        return gs

    # First pass: CODE's own directory (best chance of a matching pair).
    for g in _name_guesses_in(os.path.dirname(code_src)):
        if g != code_src and os.path.exists(g):
            return g
    # Second pass: every aarch64 EFI search directory + its known
    # subdirectories (AAVMF, edk2/aarch64, qemu, qemu-efi-aarch64).
    for d in _aarch64_efi_search_dirs(qemu_bin):
        for sub in ("", "AAVMF", os.path.join("edk2", "aarch64"),
                    "qemu", "qemu-efi-aarch64"):
            for g in _name_guesses_in(os.path.join(d, sub)):
                if g != code_src and os.path.exists(g):
                    return g
    return ""


# Legacy alias kept for any in-tree references.
AARCH64_EFI_CANDIDATES = []


def build_qemu_args(media_kind=None, media_path=None):
    """Build the full QEMU argv. media_kind is None / 'cdrom' / 'disk'."""
    osname = env("VM_OS_NAME")
    arch = env("VM_ARCH") or "x86_64"
    qcow = wf("%s.qcow2" % osname)
    sshport = read_state(osname, "sshport") or "22"

    monport = free_port(4444, 4544); write_state(osname, "monport", monport)
    serport = free_port(7000, 9000); write_state(osname, "serport", serport)
    vncport = free_port(5900, 5999); write_state(osname, "vncport", vncport)
    serlog = serial_log(osname)
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
    # VM_MEMORY honours per-conf overrides; the default 6144 covers the
    # bulk of guests. RISC-V virt machines place the FDT blob near the top
    # of RAM, and Ubuntu 22.04 riscv64's u-boot puts it right at the 8 GB
    # boundary -- with -m 6144 the FDT lands BEYOND our allocated RAM and
    # u-boot bails with "Failed to reserve memory for fdt". Per-conf
    # VM_MEMORY=8192 (or similar) sidesteps that without bumping every
    # other guest's footprint.
    a += ["-name", osname, "-m", env("VM_MEMORY") or "6144",
          "-smp", env("VM_CPU") or "2",
          "-rtc", "base=%s,clock=host,driftfix=slew" % rtc_base]
    # slirp DHCP pinned to .254 (see SLIRP_EXPECTED_GUEST_IP for rationale).
    # hostfwd target is the explicit guest IP so port forwarding never races
    # the guest's DHCP-bound state.
    #
    # VM_TRANSPORT=telnet (plan9): the guest has no sshd, the build drives it
    # over telnetd instead, so the "ssh" hostfwd points at guest port 23. An
    # optional VM_9P_PORT adds a second forward to the guest's exportfs 9P
    # listener on 564 (used to move files in/out without scp).
    guest_ctl_port = "23" if env("VM_TRANSPORT") == "telnet" else "22"
    netdev = ("user,id=net0,net=192.168.122.0/24,host=192.168.122.1,"
              "dhcpstart=192.168.122.254,ipv6=off,"
              "hostfwd=tcp:127.0.0.1:%s-192.168.122.254:%s"
              % (sshport, guest_ctl_port))
    if env("VM_9P_PORT"):
        netdev += (",hostfwd=tcp:127.0.0.1:%s-192.168.122.254:564"
                   % env("VM_9P_PORT"))
    a += ["-netdev", netdev]
    # virtio-rng-pci for all guests EXCEPT solaris, sparc64 and s390x --
    # Solaris does not have a virtio-rng driver and the unrecognized device
    # disrupts early boot; the QEMU sun4u (sparc64) machine has no free PCI
    # slot for it ("PCI: no slot/function available for virtio-rng-pci") and
    # NetBSD/sparc64 has no virtio bus on sun4u anyway, so QEMU would abort
    # at launch; s390x devices live on the CCW bus (its branch adds
    # virtio-rng-ccw instead). Mirrors anyvm.py:5627.
    if osname != "solaris" and arch not in ("sparc64", "s390x"):
        a += ["-object", "rng-builtin,id=rng0",
              "-device", "virtio-rng-pci,rng=rng0,max-bytes=1024,period=1000"]

    # Optional firmware override. A conf can point VM_BIOS at a file
    # (relative to the repo root the pipeline runs in) to replace the
    # firmware QEMU would load for the machine type. Needed by
    # openbsd/sparc64: the OpenBIOS bundled with QEMU crashes every
    # OpenBSD >= 7.3 kernel on cold boot (call-method catch result stored
    # past the client's argument array when nreturns == 0) and names the
    # IDE channel nodes "ide" instead of OBP's "ata", which breaks the
    # kernel's root-device autodetection. openbsd-builder vendors a
    # patched blob; see its bios/README.md for provenance and rebuild
    # instructions.
    if env("VM_BIOS"):
        a += ["-bios", env("VM_BIOS")]

    if arch == "aarch64":
        efi = wf("%s-QEMU_EFI.fd" % osname)
        varsf = wf("%s-QEMU_EFI_VARS.fd" % osname)
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
            vars_src = _find_aarch64_efi_vars(code_src, resolve_qemu_bin())
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
        # Per-conf override. Empirically -cpu max enables SVE/SME features
        # that newer shim/grub binaries (Ubuntu 26.04 resolute aarch64)
        # mishandle -- the EFI hangs right after BdsDxe "starting Boot0003
        # Ubuntu" with no further output. -cpu cortex-a72 lacks those
        # features and boots straight to userspace. VM_CPU_MODEL lets a
        # single conf opt out of -cpu max without rewriting the default
        # for every other guest.
        if env("VM_CPU_MODEL"):
            cpu = env("VM_CPU_MODEL")
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
        # -cpu rv64 (plain RV64GC) covers most guests. VM_CPU_MODEL lets a
        # conf pick a richer model: Ubuntu 26.04 riscv64 userspace is built
        # for the RVA23 profile baseline, so init dies with SIGILL
        # (do_trap_insn_illegal) under rv64 -- it needs -cpu rva23s64,
        # which only exists in QEMU >= 9.1 (pair it with VM_QEMU_BIN /
        # VM_QEMU_TAR; the runner's stock QEMU 8.2 additionally cannot
        # boot 26.04's kernel 7.0 at all -- it hangs at entry with zero
        # output).
        rcpu = env("VM_CPU_MODEL") or "rv64"
        a += ["-machine", "virt,accel=tcg,usb=on,acpi=off", "-cpu", rcpu]
        # NetBSD/riscv GENERIC64 drives virtio over MMIO, not PCI: virtio-blk-pci
        # enumerates "not configured" so the kernel can't find root ("boot
        # device: <unknown>" -> root device prompt). Use the MMIO virtio-*-device
        # variants for NetBSD; Ubuntu riscv64 keeps PCI.
        nb = env("VM_OS_NAME") == "netbsd"
        netdev = "virtio-net-device,netdev=net0" if nb else "%s,netdev=net0" % nic
        a += ["-device", "qemu-xhci", "-device", netdev]
        if not nb:
            a += ["-device", "virtio-balloon-pci"]
        # Use uboot.elf (ELF with proper entry point + segments), NOT the raw
        # u-boot.bin -- Ubuntu's official RISC-V QEMU docs recommend the ELF
        # variant.
        #
        # VM_UBOOT lets a conf swap in a different u-boot build (a file
        # committed in the builder repo, e.g. under files/ -- no download).
        # Needed for Ubuntu 22.04 jammy: its riscv64 cloud image boots via
        # u-boot's extlinux/sysboot path (/boot on the root partition, EMPTY
        # ESP -- no grub at all), and u-boot >= 2024.10's LMB rework made the
        # in-place FDT reservation fail there ("Failed to reserve memory for
        # fdt at 0x<RAM-top-ish> / FDT creation failed! hanging..."): the
        # qemu-riscv default env pins fdt_high=0xffffffffffffffff (never
        # relocate the FDT), and the working FDT lives inside u-boot's own
        # pre-reserved region, so lmb_reserve() now returns -EEXIST at ANY
        # -m size. The noble GA u-boot 2024.01 predates the rework and boots
        # the same image unattended (empirically verified). 24.04/26.04
        # images boot via the EFI grub path instead and never run that code.
        uboot = env("VM_UBOOT") or "/usr/lib/u-boot/qemu-riscv64_smode/uboot.elf"
        a += ["-kernel", uboot]
        if media_kind == "disk":
            if nb:
                a += ["-drive", "file=%s,format=raw,if=none,id=inst0" % media_path,
                      "-device", "virtio-blk-device,drive=inst0"]
            else:
                a += ["-drive", "file=%s,format=raw,if=virtio" % media_path]
        elif media_kind == "cdrom":
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0,media=cdrom" % media_path]
            a += ["-device", "usb-storage,drive=inst0"]
        if nb:
            a += ["-drive", "file=%s,format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap" % qcow,
                  "-device", "virtio-blk-device,drive=disk0"]
        else:
            a += ["-drive", "file=%s,format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap" % qcow]

    elif arch == "s390x":
        # QEMU s390-ccw-virtio (IBM Z). The bundled s390-ccw.img firmware
        # reads the zipl boot map straight off the virtio-blk disk, so a
        # cloud image boots with no external bootloader file. Every device
        # sits on the CCW bus (virtio-*-ccw), not PCI. The machine has no
        # VGA and no USB; the guest console is the SCLP line console
        # (ttysclp0), which QEMU routes through -serial -- i.e. our
        # serial0 chardev -- so the standard console-build flow works
        # unchanged (confs set VM_USE_CONSOLE_BUILD=1 / VM_NO_VNC_BUILD=1).
        # TCG only on x86 runners; the default 'qemu' CPU model boots
        # Ubuntu 24.04 to the login prompt (empirically verified), use
        # VM_CPU_MODEL to override (e.g. 'max').
        scpu = env("VM_CPU_MODEL") or "qemu"
        a += ["-machine", "s390-ccw-virtio,accel=tcg", "-cpu", scpu]
        a += ["-device", "%s,netdev=net0" % nic]
        a += ["-object", "rng-builtin,id=rng0",
              "-device", "virtio-rng-ccw,rng=rng0,max-bytes=1024,period=1000"]
        if media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=virtio" % media_path]
        elif media_kind == "cdrom":
            # No IDE/USB on this machine; attach install ISOs as scsi-cd
            # on a virtio-scsi-ccw controller.
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0,media=cdrom" % media_path]
            a += ["-device", "virtio-scsi-ccw,id=scsi0"]
            a += ["-device", "scsi-cd,bus=scsi0.0,drive=inst0,bootindex=0"]
        a += ["-drive", "file=%s,format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap" % qcow]

    elif arch == "sparc64":
        # QEMU sun4u (UltraSPARC IIi + OpenBIOS). NetBSD/sparc64 GENERIC drives
        # the onboard CMD646 PCI IDE (disk -> wd0, cdrom -> cd0) and a Sun Happy
        # Meal Ethernet (hme0); it has NO virtio at all, so the old virtio-based
        # branch never worked. Three sun4u-specific facts shape this:
        #
        #   * Console: OpenBIOS sends the console to the VGA framebuffer whenever
        #     a video device is present, and the framebuffer cannot be driven
        #     for a text build. `-vga none` removes the default VGA so OpenBIOS
        #     and the kernel fall back to ttya == com0 == our -serial chardev.
        #     (Hence sparc64 confs set VM_USE_CONSOLE_BUILD=1 / VM_NO_VNC_BUILD=1.)
        #
        #   * Memory: the sparc64 kernel's early OpenFirmware pmap bootstrap
        #     fails to claim memory above ~2 GB ("panic: Can't claim two pages
        #     of memory" -> Unhandled Exception 0x30) under OpenBIOS. The conf
        #     pins VM_MEMORY=2048; this branch never touches -m.
        #
        #   * NIC placement: the machine's onboard hme sits on the (full)
        #     primary PCI bus, so QEMU cannot auto-place a netdev-backed NIC
        #     there ("no slot/function available for sunhme"). We put our hme on
        #     the empty secondary Simba-bridge bus `pciB` and bind it to net0;
        #     the explicit -device suppresses the default onboard NIC, so the
        #     guest enumerates exactly one hme0.
        #
        # OpenBSD/sparc64 additionally needs:
        #   * a patched OpenBIOS via VM_BIOS (the bundled blob makes every
        #     >= 7.3 kernel crash on cold boot and breaks root autodetection;
        #     see openbsd-builder bios/README.md),
        #   * VM_NIC=e1000 (-> em0); ne2k_pci (-> ne0) also enumerates on
        #     pciB but wedges the cmd646 PCI-IDE into a "lost interrupt"
        #     write-timeout storm when net + disk DMA overlap, so e1000 is
        #     used instead (keeps boot clean under slow TCG),
        #   * usb=off to match the locally verified config -- OpenBIOS USB
        #     probing is dead weight on a serial-only machine. NetBSD was
        #     verified with the default usb=on, so only openbsd gets the
        #     flag.
        sparc_mopts = "sun4u"
        if osname == "openbsd":
            sparc_mopts += ",usb=off"
        a += ["-machine", sparc_mopts, "-vga", "none"]
        # NIC model defaults to the onboard sunhme (hme0); a conf may override
        # via VM_NIC (e.g. e1000 -> wm0). Either way it goes on the empty pciB.
        sparc_nic = env("VM_NIC") or "sunhme"
        a += ["-device", "%s,netdev=net0,bus=pciB" % sparc_nic]
        # Main disk on IDE primary master -> wd0. qcow2 rides the cmd646 fine.
        a += ["-drive", "file=%s,format=qcow2,if=ide,index=0" % qcow]
        if media_kind == "cdrom":
            # Install DVD on IDE secondary master -> cd0; boot from it.
            a += ["-drive", "file=%s,format=raw,if=ide,index=2,media=cdrom" % media_path]
            a += ["-boot", "order=d"]
            if osname == "openbsd":
                # Boot the install RAMDISK kernel (/bsd on the sparc64 CD) with
                # the -c flag so it stops in UKC before autoconfiguration. The
                # install hook (host_installOpts.py) then forces the root disk
                # to PIO/no-DMA there, so the install's concurrent CD-read +
                # disk-write does not wedge the cmd646 into a "lost interrupt"
                # write-timeout storm. CDROM-only: a disk boot must NOT carry
                # "-c" or every post-install boot would drop into UKC.
                a += ["-prom-env", "boot-file=bsd -c"]
        elif media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=ide,index=2" % media_path]
            a += ["-boot", "order=c"]
        else:
            a += ["-boot", "order=c"]

    elif arch in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        # QEMU pseries (sPAPR / PAPR) machine + SLOF firmware (auto-loaded
        # by QEMU from its bundled /usr/share/qemu/slof.bin -- no -bios). This
        # is the FreeBSD / Linux guest target on ppc64; powernv* is OPAL
        # bare-metal and won't boot a stock distro install ISO.
        #
        # Wired-up guests on this machine:
        #  * FreeBSD/powerpc64 -- BIG-ENDIAN (ELFv1). Its little-endian
        #    port (powerpc64le, ELFv2) is NOT buildable: that kernel takes
        #    an early Program Exception under QEMU TCG (illegal instruction
        #    at the VSX-unavailable vector) on every -cpu power8/9/10/max
        #    with QEMU 8.2.2 -- it would only boot on real POWER + KVM.
        #  * Ubuntu ppc64el (VM_ARCH=ppc64le) -- the Linux pseries kernel
        #    has no such problem; the cloud image boots via SLOF -> grub
        #    (PReP partition) -> kernel with console on the spapr-vty
        #    (hvc0), which -serial chardev:serial0 already routes.
        #
        # Console: -serial chardev:serial0 is routed by pseries to the SPAPR
        # virtual teletype (spapr-vty), which the guest enumerates as
        # /dev/ttyu0 on FreeBSD (uart(4) over vio). Same console-build
        # workflow as aarch64 / sparc64; the conf sets VM_USE_CONSOLE_BUILD=1
        # + VM_NO_VNC_BUILD=1 because pseries' VGA framebuffer (cirrus/std)
        # is not a real text console.
        #
        # CPU: power9 covers PowerISA 3.0, runs well under TCG, and matches
        # FreeBSD/powerpc64's POWER8+ baseline. Override with VM_CPU_MODEL.
        # cap-cfpc=broken,cap-sbbc=broken,cap-ibs=broken,cap-ccf-assist=off
        # silence harmless "TCG doesn't support requested feature" warnings
        # that pseries-noble emits for Spectre mitigations under TCG.
        mopts = ("pseries,accel=%s,usb=off"
                 ",cap-cfpc=broken,cap-sbbc=broken,cap-ibs=broken"
                 ",cap-ccf-assist=off") % accel
        cpu = env("VM_CPU_MODEL") or "power9"
        a += ["-machine", mopts, "-cpu", cpu]
        a += ["-device", "%s,netdev=net0" % nic]
        a += ["-drive", "file=%s,format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap" % qcow]
        if media_kind == "cdrom":
            # SLOF picks the bootable CDROM by bootindex. virtio-scsi-pci +
            # scsi-cd is the cleanest path (matches what the smoke-test boot
            # of FreeBSD 15.0 powerpc64 disc1.iso exercised).
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0,media=cdrom" % media_path]
            a += ["-device", "virtio-scsi-pci,id=scsi0"]
            a += ["-device", "scsi-cd,bus=scsi0.0,drive=inst0,bootindex=0"]
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=1"]
        elif media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=none,id=inst0" % media_path]
            a += ["-device", "virtio-blk-pci,drive=inst0,bootindex=0"]
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=1"]
        else:
            a += ["-device", "virtio-blk-pci,drive=disk0,bootindex=0"]

    else:
        # x86_64 (and any other PC-class arch).
        pc_mopts = "pc,accel=%s,hpet=off,smm=off,graphics=on,vmport=off,usb=on" % accel
        if osname == "hurd":
            # gnumach requires the HPET: hpet_init asserts hpet_addr != 0 and
            # panics the kernel when the machine is launched with hpet=off.
            # The amd64 build additionally needs the q35 machine: on i440fx
            # 'pc', rumpdisk's piix IDE DMA cannot address 64-bit physical
            # pages, so >= 3584 MB RAM fails root mounting with "ext2fs:
            # part:1:device:wd0: Input/output error" (bug-hurd 2025-11
            # msg00017; -M q35 is the upstream-confirmed fix). i386 keeps pc
            # (in-kernel gnumach IDE, 2 GB mem cap).
            hurd_mtype = "pc" if arch == "i386" else "q35"
            pc_mopts = ("%s,accel=%s,smm=off,graphics=on,vmport=off,usb=on"
                        % (hurd_mtype, accel))
        a += ["-machine", pc_mopts]
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
                if host_nested_amd_with_avx512():
                    # See host_nested_amd_with_avx512(): nested AMD-V corrupts
                    # guest AVX512 XSAVE state; drop just avx512f so glibc
                    # falls back to its AVX2 paths.
                    cpu += ",-avx512f"
                    log("Nested AMD KVM detected: dropping AVX512 from -cpu host")
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
        # CDROM / disk IDE-slot placement -- two layouts depending on whether
        # the main disk itself sits on the IDE bus.
        #
        #  * Disk NOT on IDE (e.g. NetBSD, which has no VM_DISK and so defaults
        #    to virtio). Pin the install CDROM to IDE primary master (index=0),
        #    matching the libvirt-era release XML (`<target dev='hda'
        #    bus='ide'/>`) so the guest sees it as cd0a -- NetBSD sysinst's
        #    default mount path. QEMU's `-cdrom` shortcut would instead land it
        #    at index=2 (cd1a), which NetBSD fails to mount, looping in the
        #    "Distribution medium" menu. The virtio disk never shares the IDE
        #    bus, so this cannot disturb the disk's identity.
        #
        #  * Disk ON IDE (dragonflybsd / ghostbsd: VM_DISK=ide). The disk MUST
        #    stay on IDE primary master (index=0) so QEMU hands it the lowest
        #    auto serial (QM00001) in BOTH the install run (CDROM present) and
        #    the startVM reboot (CDROM gone). QEMU assigns `QM%05d` serials in
        #    IDE init order (index 0,1,2,3), independent of disk-vs-cdrom. If
        #    the CDROM stole index=0 the disk would be QM00003 during install
        #    but QM00001 on reboot, so DragonFly's recorded root device
        #    `serno/QM00003` would vanish -> "Root mount failed: 6" hang at
        #    mountroot>. So here the CDROM goes on secondary master via the
        #    `-cdrom` shortcut (index=2) and the disk keeps index=0.
        ide_disk = (dif == "ide")
        if dif == "sata":
            a += ["-drive", "file=%s,format=qcow2,if=none,id=disk0,discard=unmap,detect-zeroes=unmap" % qcow]
            a += ["-device", "ich9-ahci,id=ahci0", "-device", "ide-hd,bus=ahci0.0,drive=disk0"]
        else:
            a += ["-drive", "file=%s,format=qcow2,if=%s,discard=unmap,detect-zeroes=unmap" % (qcow, dif)]
        if media_kind == "cdrom":
            if ide_disk:
                # IDE disk already holds index=0; CDROM goes to secondary
                # master (index=2) so the disk keeps its stable serial.
                a += ["-cdrom", media_path, "-boot", "order=dc,menu=off"]
            else:
                a += ["-drive", "file=%s,format=raw,if=ide,index=0,media=cdrom" % media_path,
                      "-boot", "order=dc,menu=off"]
        elif media_kind == "disk":
            a += ["-drive", "file=%s,format=raw,if=ide" % media_path]
        # VGA device, anyvm.py:5441-5444. NetBSD/Haiku -> std,
        # OpenBSD desktop -> cirrus, default -> virtio (= virtio-vga on x86).
        a += ["-vga", vga_type()]

    # Optional cloud-init NoCloud seed: when VM_SEED_ISO names an existing
    # file (generated at build time by a prepareImage hook -- never committed
    # to git), attach it as a second CDROM on every boot so cloud-init in
    # BASIC-CLOUDINIT guest images can pick up its user-data on first boot.
    # No bootindex / not in -boot order: firmware keeps booting the disk; the
    # seed is data-only. Re-attaching on later boots is harmless (cloud-init
    # runs once per instance-id, and the hook's user-data disables cloud-init
    # in the final artifact anyway). Gated on the env var, so every conf that
    # does not set VM_SEED_ISO is completely unaffected.
    seed = env("VM_SEED_ISO")
    if seed and os.path.exists(seed):
        if arch == "aarch64":
            a += ["-drive", "file=%s,format=raw,if=none,id=seed0,media=cdrom" % seed,
                  "-device", "usb-storage,drive=seed0"]
        else:
            a += ["-drive", "file=%s,format=raw,if=ide,index=2,media=cdrom" % seed]

    # VNC display number = port - 5900 (display :N <-> TCP 5900+N).
    a += ["-display", "vnc=127.0.0.1:%d" % (vncport - 5900)]
    if not console and arch != "aarch64":
        a += ["-device", "usb-tablet"]
    return a


# ----------------------------------------------------------------------------
# Guest hardware profile
# ----------------------------------------------------------------------------
#
# build_qemu_args() (above) and anyvm.py's launch path independently decide the
# guest's QEMU hardware shape -- machine type, CPU model, disk bus, NIC, VGA,
# RNG, firmware kind -- from the same per-(os,arch,release) facts. They were
# kept in lock-step BY HAND (every helper here and in build_qemu_args carries a
# "Mirrors anyvm.py:NNNN" note), and they silently drifted: an image would
# build + verify green here, then fail to boot under anyvm.py because the two
# argv assemblers disagreed on one device.
#
# build_guest_profile() captures those decisions ONCE, at build time, from the
# code that actually produced the image, and exportOVA() ships it beside the
# qcow2 as <output>.profile.json (a published release asset). anyvm.py reads it
# and drives its launch from this single source of truth, so a new guest only
# needs its conf + build.py -- never a parallel edit in anyvm.py.
#
# The profile records ONLY host-independent guest-shape facts. Everything that
# depends on the MACHINE RUNNING the VM stays owned by anyvm.py: the QEMU
# binary, acceleration (kvm/hvf/whpx/tcg) and the host-tied `-cpu host` model,
# firmware file discovery, ports, display/VNC, and every user --flag override.

GUEST_PROFILE_VERSION = 1


def _profile_machine():
    """(machine_type, machine_opts) with accel EXCLUDED -- the host picks the
    accelerator at run time. Mirrors the -machine strings build_qemu_args()
    assembles per arch."""
    arch = env("VM_ARCH") or "x86_64"
    osname = env("VM_OS_NAME")
    if arch == "aarch64":
        opts = "gic-version=3,usb=on"
        if obsd_acpi_off():
            opts += ",acpi=off"
        return "virt", opts
    if arch == "riscv64":
        return "virt", "usb=on,acpi=off"
    if arch == "s390x":
        return "s390-ccw-virtio", ""
    if arch == "sparc64":
        return "sun4u", ("usb=off" if osname == "openbsd" else "")
    if arch in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        return "pseries", ("usb=off,cap-cfpc=broken,cap-sbbc=broken,"
                           "cap-ibs=broken,cap-ccf-assist=off")
    if osname == "hurd":
        # gnumach requires the HPET; amd64 additionally needs q35 (see the
        # hurd branch in build_qemu_args).
        return ("pc" if arch == "i386" else "q35",
                "smm=off,graphics=on,vmport=off,usb=on")
    return "pc", "hpet=off,smm=off,graphics=on,vmport=off,usb=on"


def _profile_cpu_model():
    """Guest-mandated CPU MODEL (the software model), or None to let anyvm.py
    pick a host-aware one. A VM_CPU_MODEL conf override wins. build_qemu_args()
    uses `host` under KVM/HVF, but that is host-specific and is NEVER recorded:
    anyvm.py keeps `host` under hardware accel and falls back to this model
    under TCG (the common case on an end user's machine)."""
    m = env("VM_CPU_MODEL")
    if m:
        return m
    arch = env("VM_ARCH") or "x86_64"
    osname = env("VM_OS_NAME")
    if arch == "aarch64":
        return "neoverse-n1" if osname == "openbsd" else "max"
    if arch == "riscv64":
        return "rv64"
    if arch == "s390x":
        return "qemu"
    if arch in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
        return "power9"
    # sparc64 (sun4u carries no -cpu) and x86_64 (anyvm.py picks qemu64 under
    # TCG, host/Broadwell-v4 under KVM -- all host-aware) record no model.
    return None


def _profile_rng():
    """virtio-rng transport: 'pci', 'ccw' (s390x), or 'none'. Mirrors the
    build_qemu_args() rng gating (skipped for solaris and sparc64; CCW bus on
    s390x)."""
    arch = env("VM_ARCH") or "x86_64"
    if arch == "s390x":
        return "ccw"
    if env("VM_OS_NAME") == "solaris" or arch == "sparc64":
        return "none"
    return "pci"


def _profile_firmware_kind():
    """Firmware ROLE the guest needs (not a path -- anyvm.py discovers the
    actual file on the host): 'bios' (explicit -bios override, e.g. the patched
    OpenBIOS for openbsd/sparc64), 'edk2-pflash' (aarch64), 'uboot' (riscv64
    -kernel payload; anyvm.py may prefer EDK2 when present), or 'default'
    (firmware QEMU bundles for the machine -- SeaBIOS / SLOF / s390-ccw)."""
    arch = env("VM_ARCH") or "x86_64"
    if env("VM_BIOS"):
        return "bios"
    if arch == "aarch64":
        return "edk2-pflash"
    if arch == "riscv64":
        return "uboot"
    return "default"


def _profile_qemu_min_version():
    """Minimum QEMU version this guest needs ("X.Y[.Z]"), or None when the
    runner's stock QEMU sufficed. Parsed from the conf's VM_QEMU_TAR /
    VM_QEMU_BIN pin: the builder pins a newer QEMU for guests the distro's
    stock build cannot run (ubuntu ppc64le 22.04 / s390x all / riscv64 26.04 --
    TCG miscompiles or a missing CPU model). anyvm.py must apply the same floor
    (its ensure_pinned_qemu downloads a matching build) or the guest boots on a
    too-old QEMU and fails -- a classic build-green / run-broken split."""
    src = env("VM_QEMU_TAR") or env("VM_QEMU_BIN") or ""
    m = re.search(r"qemu-(\d+\.\d+(?:\.\d+)?)", src)
    return m.group(1) if m else None


def _profile_balloon():
    """Whether build_qemu_args() attaches virtio-balloon-pci. x86 always; riscv64
    except NetBSD (its GENERIC64 cannot drive the PCI balloon); never on aarch64
    / s390x / sparc64 / pseries."""
    arch = env("VM_ARCH") or "x86_64"
    if arch in ("x86_64", "i386"):
        # i386 (hurd) runs through the same PC-class else-branch of
        # build_qemu_args(), which attaches the balloon unconditionally.
        return True
    if arch == "riscv64" and env("VM_OS_NAME") != "netbsd":
        return True
    return False


def build_guest_profile():
    """Normalized, host-independent description of the guest's QEMU hardware
    shape (see the section comment above). Reuses the same helpers
    build_qemu_args() calls -- net_card(), disk_if(), vga_type(),
    obsd_acpi_off() -- so disk/NIC/VGA/ACPI carry ZERO drift; the rest mirrors
    build_qemu_args()'s per-arch blocks."""
    arch = env("VM_ARCH") or "x86_64"
    osname = env("VM_OS_NAME")
    mtype, mopts = _profile_machine()
    # NetBSD/riscv64 GENERIC64 has no PCI virtio bus, so build_qemu_args() drives
    # virtio over the MMIO transport there (virtio-blk-device / virtio-net-device).
    mmio = (osname == "netbsd" and arch == "riscv64")
    nic = net_card()
    if mmio and nic == "virtio-net-pci":
        nic = "virtio-net-device"
    # disk bus. build_qemu_args()'s sparc64 (sun4u) branch hardwires the disk
    # to IDE (wd0) and never consults disk_if(), whose generic default would
    # say virtio -- record the real bus here.
    dif = "ide" if arch == "sparc64" else disk_if()
    # Effective VGA device, normalized the way build_qemu_args() emits it.
    if arch == "sparc64":
        vga = "none"
    elif arch in ("riscv64", "s390x", "powerpc64", "powerpc64le",
                  "ppc64", "ppc64le"):
        vga = None             # console-only arches add no video device
    elif arch == "aarch64":
        v = vga_type()
        vga = "virtio-gpu-pci" if v in ("virtio", "virtio-gpu", "std", "") else v
    else:
        vga = vga_type()       # x86: std / cirrus / virtio
    # Hard guest limits. sun4u (sparc64) is uniprocessor and its early
    # OpenFirmware pmap bootstrap panics above ~2 GB (1 GB for openbsd).
    mem_cap = None
    cpu_cap = None
    if arch == "sparc64":
        mem_cap = 1024 if osname == "openbsd" else 2048
        cpu_cap = 1
    if osname == "hurd":
        # Stock gnumach is uniprocessor (SMP is an experimental add-on
        # package), and the 32-bit i386 kernel cannot address big RAM.
        cpu_cap = 1
        if arch == "i386":
            mem_cap = 2048
    return {
        "anyvm_profile_version": GUEST_PROFILE_VERSION,
        "os": osname,
        "arch": arch,
        "release": env("VM_RELEASE"),
        "machine_type": mtype,
        "machine_opts": mopts,
        "cpu_model": _profile_cpu_model(),
        "disk_if": dif,
        "virtio_transport": "mmio" if mmio else "pci",
        "net_card": nic,
        "net_bus": "pciB" if arch == "sparc64" else None,
        "vga": vga,
        "rng": _profile_rng(),
        "firmware_kind": _profile_firmware_kind(),
        "qemu_min_version": _profile_qemu_min_version(),
        "balloon": _profile_balloon(),
        # RTC epoch the guest expects: windows/haiku read the CMOS clock as
        # local time, everything else as UTC. Mirrors build_qemu_args()'s
        # rtc_base and anyvm.py's.
        "rtc_base": "localtime" if osname in ("windows", "haiku") else "utc",
        "console": bool(env("VM_USE_CONSOLE_BUILD")),
        # Remote-exec channel into the guest: "ssh" for everything except
        # VM_TRANSPORT=telnet guests (plan9: telnetd on 23, exportfs 9P on
        # 564, no sshd at all).
        "transport": "telnet" if env("VM_TRANSPORT") == "telnet" else "ssh",
        "mem_cap_mb": mem_cap,
        "cpu_cap": cpu_cap,
    }


def _profile_sanity_check(profile, cmdline_path):
    """Best-effort same-file drift guard: warn (never fail) when a profile
    value is absent from the QEMU command line build_qemu_args() actually
    launched. Catches build_guest_profile() falling out of step with
    build_qemu_args() at CI time, where it is cheap to notice."""
    try:
        with open(cmdline_path) as f:
            cl = f.read()
    except OSError:
        return
    checks = [("machine_type", profile.get("machine_type")),
              ("cpu_model", profile.get("cpu_model")),
              ("net_card", profile.get("net_card"))]
    for name, val in checks:
        if val and val not in cl:
            log("WARNING: guest profile %s=%r not found in launched cmdline "
                "(build_guest_profile drifted from build_qemu_args?)" % (name, val))


def launch_qemu(media_kind=None, media_path=None):
    """Launch QEMU detached so it survives this Python process."""
    osname = env("VM_OS_NAME")
    qbin = resolve_qemu_bin()
    cmd = [qbin] + build_qemu_args(media_kind, media_path)
    with open(state(osname, "cmdline"), "w") as f:
        f.write(" ".join(cmd) + "\n")
    log("Launching QEMU for %s:" % osname)
    log(" ".join(cmd))
    qemulog = wf("%s.qemu.log" % osname)
    logf = open(qemulog, "ab")
    p = subprocess.Popen(cmd, stdin=DEVNULL, stdout=logf, stderr=logf,
                         start_new_session=True)
    write_state(osname, "pid", p.pid)
    time.sleep(1)
    if p.poll() is not None:
        log("QEMU failed to start for %s; tail of %s:" % (osname, qemulog))
        log(tail_file(qemulog, 50))
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
    expected = end - start + 1
    last_err = None
    for attempt in range(DL_ATTEMPTS):
        try:
            written = 0
            with _http_get_stream(url, start, end) as resp, open(fpath, "r+b") as f:
                f.seek(start)
                while True:
                    buf = resp.read(DL_BUF)
                    if not buf: break
                    f.write(buf)
                    written += len(buf)
            # A short read (server/connection ends the range early without an
            # error) must NOT count as success: download() pre-truncates the file
            # to the full size, so a short chunk silently leaves zeros in its
            # region and corrupts the image undetectably (the final size check
            # always passes on the pre-sized file). Fail loudly so the retry loop
            # -- and ultimately download()'s rc -- catches it.
            if written != expected:
                raise IOError("short chunk [%d-%d]: got %d of %d bytes" % (
                    start, end, written, expected))
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


def _download_resume(url, fpath, total):
    """Single-connection download that RESUMES via Range after a dropped or
    short connection. Needed for mirrors like archive.netbsd.org that cap each
    connection at a few MB and reset it mid-transfer (seen as ~9 MiB then SSL
    UNEXPECTED_EOF / HTTP 402). Keeps re-requesting `Range: bytes=<got>-` until
    the full size arrives; gives up only after DL_ATTEMPTS rounds with no
    progress."""
    open(fpath, "wb").close()   # start from an empty file
    got = 0
    stalls = 0
    while got < total:
        start = got
        try:
            with _http_get_stream(url, got, None) as resp, open(fpath, "r+b") as f:
                f.seek(got)
                while True:
                    buf = resp.read(DL_BUF)
                    if not buf: break
                    f.write(buf)
                    got += len(buf)
        except Exception as e:
            log("download stream error at %d/%d: %s (resuming)" % (got, total, e))
        if got > start:
            stalls = 0
        else:
            stalls += 1
            if stalls >= DL_ATTEMPTS:
                raise IOError("download stalled at %d/%d bytes" % (got, total))
            time.sleep(2 * stalls)


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
            log("FATAL: download failed: %s -- %s" % (link, e)); sys.exit(1)
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
    if rc != 0:
        # Some mirrors (e.g. archive.netbsd.org) rate-limit / reject concurrent
        # Range requests -- observed as "HTTP 402 Payment Required" on 7 of 8
        # chunks, or each connection capped at ~9 MiB then reset. Retry the whole
        # file as a single stream WITH resume, which stays under the per-client
        # limit and reconnects through mid-transfer drops.
        log("multi-threaded download failed; retrying single-stream with resume...")
        try:
            if size is not None and ranges_ok:
                _download_resume(link, fileout, size)
            else:
                _download_single(link, fileout)
        except Exception as e:
            log("FATAL: download failed: %s -- %s" % (link, e))
            sys.exit(1)
    try:
        actual = os.path.getsize(fileout)
        if actual != size:
            log("FATAL: download size mismatch for %s: expected %d, got %d" % (
                link, size, actual))
            sys.exit(1)
    except OSError:
        pass
    log("Download finished")
    return 0


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
    return wf("%s.serial.log" % osname)


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
        # Emit one byte at a time with a small inter-byte delay. Raw-burst
        # sendall() makes QEMU forward the whole string into the guest UART
        # within microseconds, which races getty/login on slow consoles
        # (NetBSD evbarm plcom0, OpenBSD wscons, ...) and the line discipline
        # silently drops the tail. The 40ms cadence matches what vncdotool
        # uses (--delay=40) and is empirically slow enough that every byte
        # is echoed and acted on by login. Override with VM_CONSOLE_DELAY
        # (ms) if some guest needs slower.
        try:
            delay = float(env("VM_CONSOLE_DELAY") or "40") / 1000.0
        except (TypeError, ValueError):
            delay = 0.040
        with self._send_lock:
            try:
                for b in data:
                    self.ser.sendall(bytes([b]))
                    if delay > 0:
                        time.sleep(delay)
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
    """Install host dependencies. Each package-manager step is echoed to the
    log as it starts (and whatever it prints streams live), so a slow or hung
    install is identifiable from the last command shown instead of the phase
    sitting silent. The apt -q / pip -q flags stay on, so the log is not
    flooded with per-package chatter -- only the step markers and any errors
    appear. Alias the quiet run-helpers to their loud variants for the length
    of this function so the call sites below need no change."""
    _run_quiet = _run_loud
    _sh_quiet = _sh_loud
    log("setup: installing host dependencies (each step echoed below)")
    if is_linux():
        apt_env = dict(os.environ)
        apt_env["DEBIAN_FRONTEND"] = "noninteractive"
        _run_quiet(["sudo", "-E", "apt-get", "update", "-q"], env=apt_env)
        _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                    "zstd", "qemu-utils", "qemu-system-x86", "sshpass",
                    "netcat-openbsd"], env=apt_env)
        if install_ocr:
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "tesseract-ocr", "python3-pil",
                        "tesseract-ocr-eng", "python3-pip"], env=apt_env)
            # Use opencv-python-HEADLESS, not the full opencv-python wheel: the
            # full wheel needs libGL.so.1, which headless CI runners (GitHub
            # Actions) lack, so `import cv2` raises ImportError there. cv2 is
            # used by ocr_py and transitively by paddleocr, so a clean import
            # matters either way.
            pip = sys.executable + " -m pip install -q"
            if _sh_quiet(pip + " --break-system-packages "
                         "pytesseract opencv-python-headless vncdotool") != 0:
                _sh_quiet(pip + " pytesseract opencv-python-headless vncdotool")
            if env("VM_OCR") == "paddle":
                # Neural OCR (see ocr_paddle) for installer dialogs whose dim /
                # low-contrast text tesseract cannot read. Install the engine,
                # then warm it once so the PP-OCRv6 medium models download here
                # during setup rather than stalling the first waitForText poll.
                # Use `sys.executable -m pip`, not bare `pip3`: on GitHub
                # runners pip3 and the python3 running build.py can resolve to
                # different interpreters / site-packages.
                log("setup: installing PaddleOCR -- the paddlepaddle wheel is "
                    "hundreds of MB, expect this step to take a few minutes")
                if _sh_quiet(pip + " --break-system-packages "
                             "paddlepaddle 'paddleocr>=3.7'") != 0:
                    _sh_quiet(pip + " paddlepaddle 'paddleocr>=3.7'")
                # pip lands paddle in the user site-packages
                # (~/.local/lib/pythonX.Y/site-packages). On a fresh CI runner
                # that directory does not exist when this interpreter starts, so
                # site.py never puts it on sys.path; pip then creates it mid-run,
                # but this process's sys.path is already fixed, so every
                # `import paddleocr` fails with ModuleNotFoundError even though
                # pip returned 0 (exactly the CI failure: all OCR fell back to
                # tesseract and hung at the hostname dialog). Add the user site
                # to THIS process's path and clear the import caches so the
                # warm-up below and every later ocr_paddle in this same run can
                # import it.
                try:
                    import site, importlib
                    us = site.getusersitepackages()
                    if us:
                        site.addsitedir(us)        # process any .pth there
                        # Put user site at the FRONT, not the end: a stale apt
                        # dep already on sys.path (e.g. an old typing_extensions
                        # in dist-packages) would otherwise shadow the newer one
                        # pip just installed for paddle. This restores the
                        # priority site.py would have given it had the dir
                        # existed at interpreter startup.
                        if us in sys.path:
                            sys.path.remove(us)
                        sys.path.insert(0, us)
                    importlib.invalidate_caches()
                except Exception as e:
                    log("setup: could not add user site to sys.path (%s)" % e)
                try:
                    import importlib.util, cv2, numpy
                    cv2.imwrite("/tmp/_paddle_warm.png",
                                numpy.full((60, 200, 3), 255, numpy.uint8))
                    ocr_paddle("/tmp/_paddle_warm.png")
                    if importlib.util.find_spec("paddleocr") is not None:
                        log("setup: PaddleOCR ready (models cached)")
                    else:
                        log("setup: WARNING paddleocr not importable after "
                            "install; OCR falls back to tesseract")
                except Exception as e:
                    log("setup: PaddleOCR warm-up failed (%s)" % e)
            vp = os.path.join(HOME, ".local", "bin", "vncdotool")
            if os.path.exists(vp):
                _run_quiet(["sudo", "ln", "-sf", vp, "/usr/local/bin/vncdotool"])
        if env("VM_ARCH") == "riscv64":
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "qemu-system-misc", "u-boot-qemu"], env=apt_env)
        if env("VM_ARCH") == "aarch64":
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "qemu-system-arm", "qemu-efi-aarch64"], env=apt_env)
        if env("VM_ARCH") == "s390x":
            # qemu-system-s390x ships in its own package on Ubuntu (NOT in
            # qemu-system-misc); its s390-ccw.img firmware comes with the
            # qemu-system-data dependency.
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "qemu-system-s390x"], env=apt_env)
        # A conf may ship its own QEMU build as a tarball (bin/ +
        # share/qemu layout, built against the runner's distro libs;
        # generated on the fly by hooks/host_beforeBuild.sh -- see
        # ubuntu-builder files/README.md and files/build-qemu10.sh) --
        # extract it here and the conf points VM_QEMU_BIN at the extracted
        # binary. Arch-independent: riscv64 (Ubuntu 26.04 needs QEMU >= 9.1
        # for -cpu rva23s64) and s390x (stock 8.2 TCG intermittently
        # freezes guest systemd) both use it. The apt qemu packages above
        # still provide the runtime libs (glib, pixman, slirp, fdt).
        if env("VM_QEMU_TAR"):
            _run_quiet(["tar", "--zstd", "-xf", env("VM_QEMU_TAR")])
        if env("VM_ARCH") == "sparc64":
            # qemu-system-sparc64 (sun4u + bundled OpenBIOS) ships in the
            # qemu-system-sparc package; no separate firmware package needed.
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "qemu-system-sparc"], env=apt_env)
        if env("VM_ARCH") in ("powerpc64", "powerpc64le", "ppc64", "ppc64le"):
            # qemu-system-ppc64 (pseries machine) ships in the qemu-system-ppc
            # package; its SLOF firmware (/usr/share/qemu/slof.bin) is bundled
            # with it, so no separate firmware package is needed. The GitHub
            # ubuntu runner image does NOT preinstall this, hence the explicit
            # apt-get (a local dev box may already have it from qemu-system).
            _run_quiet(["sudo", "-E", "apt-get", "install", "-y", "-q", "--no-install-recommends",
                        "qemu-system-ppc"], env=apt_env)
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
        _sh_quiet("pip3 install -q pytesseract opencv-python-headless vncdotool")
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

def createVM(isolink=None, sshport=None, disklink=None):
    osname = _check_osname("createVM")
    if not osname: return 1
    vdi = wf("%s.qcow2" % osname)
    iso = wf("%s.iso" % osname)
    if isolink.endswith("img"):
        iso = wf("%s.img" % osname)
    if not os.path.exists(iso):
        download(isolink, iso)
        if isolink.endswith("bz2"):
            os.rename(iso, iso + ".bz2")
            must_sh("bzip2 -dc %s > %s" % (shlex.quote(iso + ".bz2"), shlex.quote(iso)),
                    "bzip2 decompress (corrupt download?)")
    if disklink:
        if not os.path.exists(vdi):
            download(disklink, vdi)
    else:
        # Default disk is 200G (sparse), but a conf can pin a smaller size via
        # VM_DISK_SIZE. NetBSD/sparc64 needs this: the OpenFirmware FCode
        # bootblock mis-reads ofwboot from a large root FFS ("Inode not
        # directory", NetBSD PR 56363), so the sparc64 conf builds on a ~4G
        # disk. anyvm.py runs the image as-is (it never resizes), so the small
        # disk carries through to the runtime VM.
        run(["qemu-img", "create", "-f", "qcow2", "-o", "preallocation=off",
             vdi, env("VM_DISK_SIZE") or "200G"])
    try: os.chmod(vdi, 0o777)
    except OSError: pass
    write_state(osname, "sshport", sshport or "22")
    if iso.endswith("img"):
        return launch_qemu("disk", iso)
    return launch_qemu("cdrom", iso)


def createVMFromVHD(sshport=None):
    osname = _check_osname("createVMFromVHD")
    if not osname: return 1
    vhd = wf("%s.qcow2" % osname)
    must_run(["qemu-img", "resize", vhd, "+200G"], "qemu-img resize")
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
    path = serial_log(osname)
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


def _wait_vm_down(what="VM", poll=20, max_seconds=1800):
    """Block until isRunning() reports not-running. Every poll prints a one-
    line status: elapsed time, size of <osname>.serial.log, and the last non-
    empty line of the guest console -- so it's obvious whether the install is
    making progress or stuck.

    After max_seconds (default 1800 = 30 min) without the VM going down, we
    force-kill the QEMU process via destroyVM() and return. This caps the
    blast radius when a guest ignores the ACPI shutdown request -- before
    the cap, FreeBSD 13.5 aarch64 sat at its login: prompt and burned the
    entire 6h CI budget in this loop."""
    osname = env("VM_OS_NAME") or "vm"
    monport = read_state(osname, "monport")
    serport = read_state(osname, "serport")
    vncport = read_state(osname, "vncport") or "5900"
    log("waiting for %s to power off (poll %ds, max %ds; vnc 127.0.0.1::%s, "
        "monitor 127.0.0.1:%s, serial 127.0.0.1:%s -> %s.serial.log)"
        % (what, poll, max_seconds, vncport, monport, serport, osname))
    elapsed = 0
    stalled = 0
    last_size = -1
    while isRunning() == 0:
        time.sleep(poll)
        elapsed += poll
        size, tail = _serial_tail_line()
        mm, ss = divmod(elapsed, 60)
        log("[%dm%02ds] %s, serial=%dB | %s" % (mm, ss, what, size, tail[:140]))
        # Some guests cannot power off QEMU and HALT instead: NetBSD/riscv64 and
        # sparc64 have no working QEMU poweroff, so `shutdown -p` ends at "has
        # halted / press any key to reboot" with QEMU still running. Treat that
        # as down and force-kill at once rather than burning the full timeout.
        # "done halting" is plan9/9front's fshalt banner: hjfs has ended and
        # the CPU sits in a halt loop with QEMU still alive.
        if re.search(r"has halted|press any key to reboot|done halting", tail, re.I):
            log("%s: guest halted without powering off QEMU; force-killing" % what)
            destroyVM()
            return
        # Console builds only: the serial log IS the guest console there, so
        # a shutdown that stops writing to it has stopped making progress.
        # Empirically (NetBSD 10.1 sparc64): the cmd646 lost-interrupt storm
        # can wedge BEFORE "syncing disks..." ever prints and then go silent
        # forever -- the "has halted" banner never comes and the wait would
        # burn the whole max_seconds. 5 minutes of zero serial growth during
        # a shutdown means dead, not slow; force-kill right away. (VNC builds
        # are exempt: their guests console on the emulated VGA and a mute
        # serial log is normal.)
        if size != last_size:
            last_size = size
            stalled = 0
        else:
            stalled += poll
            if env("VM_USE_CONSOLE_BUILD") and stalled >= 300:
                log("%s: serial console silent for %d s; force-killing QEMU"
                    % (what, stalled))
                destroyVM()
                return
        if elapsed >= max_seconds:
            log("%s did not power off in %d s; force-killing QEMU"
                % (what, max_seconds))
            destroyVM()
            return
    log("%s powered off after %d s" % (what, elapsed))


def clearVM():
    osname = _check_osname("clearVM")
    if not osname: return 1
    if isRunning() == 0:
        destroyVM()
    closeConsole()
    for f in [wf("%s.qcow2" % osname), wf("%s.img" % osname), wf("%s.pid" % osname),
              wf("%s.monport" % osname), wf("%s.serport" % osname), wf("%s.sshport" % osname),
              wf("%s.vncport" % osname),
              wf("%s.serial.log" % osname), wf("%s.qemu.log" % osname), wf("%s.cmdline" % osname),
              wf("%s-QEMU_EFI.fd" % osname), wf("%s-QEMU_EFI_VARS.fd" % osname)]:
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


_PADDLE_OCR = None


def ocr_paddle(img):
    """OCR via PaddleOCR (PP-OCRv6 medium det + rec). Used when a
    conf sets VM_OCR=paddle. PaddleOCR's neural recognizer reads dim / low-
    contrast installer dialog text that tesseract drops entirely (e.g. the
    OmniOS "Enter the system hostname" box), so no per-screen colour tricks
    are needed. The medium tier (not small/tiny) is deliberate: the lighter
    PP-OCRv6 tiers have a systematic g->q misread on the OmniOS console font
    (login->loqin, Configure->Confiqure, Copyright->Copyriqht) that breaks
    waitForText matching; medium reads them cleanly at the cost of a heavier
    predict. The engine is built once and reused. enable_mkldnn=False avoids a
    paddlepaddle 3.x oneDNN/PIR crash; the doc-orientation / unwarp /
    textline-orientation sub-models are disabled (a flat console screen needs
    none). Falls back to tesseract on any error / if PaddleOCR is not
    installed."""
    global _PADDLE_OCR
    try:
        if _PADDLE_OCR is None:
            # Cap CPU: paddlepaddle otherwise spins ~6 OpenMP threads per
            # predict, which on a 2-4 vCPU CI runner saturates the box and
            # starves the KVM guest's boot / sshd. cpu_threads=2 plus
            # OMP_NUM_THREADS hold the thread count down while staying in
            # budget (the medium model is heavier than mobile/small, so a
            # predict runs a few seconds rather than ~1s).
            os.environ.setdefault("OMP_NUM_THREADS", "2")
            from paddleocr import PaddleOCR
            _PADDLE_OCR = PaddleOCR(
                lang="en", enable_mkldnn=False, cpu_threads=2,
                text_detection_model_name="PP-OCRv6_medium_det",
                text_recognition_model_name="PP-OCRv6_medium_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False)
        lines = []
        for r in _PADDLE_OCR.predict(img):
            try: lines.extend(r["rec_texts"])
            except Exception: pass
        return "\n".join(lines)
    except Exception as e:
        log("ocr_paddle failed (%s); falling back to tesseract" % e)
        return ocr_tess(img)


def ocr(img):
    mode = env("VM_OCR")
    if mode == "paddle":
        return ocr_paddle(img)
    if mode == "py":
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
    with open(wf("index.html"), "w") as f:
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
        with open(wf("screen.txt"), "w") as f:
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


# build.py is one long-lived process, so the dump-dedup state can simply
# remember everything ever printed across the whole pipeline run.
_screen_dump_state = {"serial_pos": {}, "ocr_seen": set()}


def _unseen_screen_text(screen):
    """Return only screen text that has not been dumped before in this
    process.

    Console-build mode ignores `screen` (a 50-line tail window) and instead
    tracks a byte offset into the serial log, which is append-only and only
    truncated when launch_qemu() restarts QEMU: every byte gets printed
    exactly once, bursts longer than the tail window are not lost, and a
    truncation (size < saved offset) resets the offset so a fresh boot is
    printed from its first line.

    OCR/VNC mode has no underlying stream, so it dedupes line-wise against
    every line printed so far: a redraw of an already-shown screen prints
    nothing, a changed menu prints just its changed lines."""
    if env("VM_USE_CONSOLE_BUILD"):
        osname = env("VM_OS_NAME") or "vm"
        path = serial_log(osname)
        posmap = _screen_dump_state["serial_pos"]
        pos = posmap.get(path, 0)
        try:
            if os.path.getsize(path) < pos:
                pos = 0  # log truncated: QEMU was relaunched
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
        except OSError:
            return ""
        posmap[path] = pos + len(data)
        return data.decode("utf-8", "replace")
    seen = _screen_dump_state["ocr_seen"]
    fresh_lines = []
    for ln in screen.splitlines():
        # Normalize the dedup key: OCR renders the same physical line with
        # jittering whitespace from capture to capture, which would make old
        # lines look new forever. Collapse whitespace runs for the key; the
        # line itself is printed in its original form.
        key = " ".join(ln.split())
        if key and key not in seen:
            seen.add(key)
            fresh_lines.append(ln)
    return "\n".join(fresh_lines)


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
        with open(wf("_screenText.txt"), "w") as f:
            f.write(screen)
        # Dump only what has never been printed before: re-printing the whole
        # 50-line window every 3 s buried the build log in repetition. The
        # match below still checks the FULL current screen, so anchors that
        # scrolled in earlier are unaffected.
        fresh = _unseen_screen_text(screen)
        if fresh.strip():
            log(""); log("==========screen Text============")
            log(fresh); log("==========screen Text end============")
        else:
            log("(no new screen text)")
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
    try: os.remove(wf("_stopvnc.txt"))
    except OSError: pass
    # The HTTP server can stay as a detached subprocess. It serves WORKDIR
    # (not the repo root) via --directory, so the screenshots / OCR text /
    # index.html we drop into WORKDIR are the console content.
    subprocess.Popen([sys.executable, "-m", "http.server", "--directory", WORKDIR],
                    stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                    start_new_session=True)
    if not os.path.exists(wf("index.html")):
        with open(wf("index.html"), "w") as f:
            f.write("<!DOCTYPE html>\n<html>\n<head>\n<title>%s</title>\n"
                    "<meta http-equiv='refresh' content='1'>\n</head>\n"
                    "<body style='background-color:grey;'>\n\n"
                    "<h1>Please just wait....<h1>\n\n</body>\n</html>\n" % osname)

    def loop():
        while not _startweb_stop.is_set():
            if not os.path.exists(wf("_stopvnc.txt")):
                try:
                    _screen_text_value(wf("screen.png"))
                except Exception:
                    pass
            time.sleep(3)
    global _startweb_thread
    _startweb_thread = threading.Thread(target=loop, daemon=True, name="startweb-loop")
    _startweb_thread.start()
    return 0


def pauseVNC():
    open(wf("_stopvnc.txt"), "w").close()
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
    # Only append if the marker line isn't already present. Without this guard
    # every rebuild duplicates the block, and after enough runs OpenSSH stops
    # honoring SendEnv (more importantly: dozens of redundant `Include` lines
    # confuse downstream parsing). Each builder rewrites its own per-osname
    # block under config.d/ below anyway, so the global block only needs to
    # exist once.
    sshconfig = os.path.join(sshdir, "config")
    marker = "SendEnv   CI  GITHUB_*"
    existing = ""
    if os.path.exists(sshconfig):
        with open(sshconfig) as f:
            existing = f.read()
    if marker not in existing:
        with open(sshconfig, "a") as f:
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


def exportOVA(ova=None, qemu_args=None):
    osname = _check_osname("exportOVA")
    if not osname: return 1
    if not ova:
        log("Usage: exportOVA out.qcow2 [out.qemu]"); return 1
    src = wf("%s.qcow2" % osname)
    log(src)
    # Stage 1: qemu-img convert the work disk into a fresh, compacted /
    # sparsified qcow2 at the release path. qemu-img refuses to use the same
    # file as both input and output, so we write to `ova` and swap below.
    # Peak disk during this step: src + ova (~2x the qcow2 size, briefly).
    must_run(["qemu-img", "convert", "-O", "qcow2", "-S", "4k",
              "-o", "preallocation=off", src, ova], "qemu-img convert (export)")
    # Stage 2: drop the original work disk and move the converted one into
    # its place. After this we hold a single qcow2 file (~1x), and the
    # downstream verification VM (started later in main()) still boots from
    # the same `<osname>.qcow2` path it always did. Without this swap, zstd
    # below runs with src + ova + the growing .zst chunks all on disk
    # simultaneously (~2.25x peak), which trips the runner's free-space
    # margin for the bigger images.
    try: os.remove(src)
    except OSError: pass
    os.rename(ova, src)
    # Stage 3: stream-compress the single remaining qcow2 to the release
    # `<output>.qcow2.zst[.N]`. split keeps any future >2GB build's chunks
    # under GitHub's release-asset size cap; single-chunk case renames
    # .zst.0 -> .zst so consumers just `zstd -d` the one file.
    # bash + pipefail so a zstd failure mid-pipe aborts (a bare pipe would
    # report only split's exit and silently ship a truncated .zst).
    must_run(["bash", "-c", "set -o pipefail; zstd -c %s | split -b 2000M -d -a 1 - %s"
              % (shlex.quote(src), shlex.quote(ova + ".zst."))], "zstd compress")
    run(["ls", "-lah"])
    try: os.rename(ova + ".zst.0", ova + ".zst")
    except OSError: pass
    sh("chmod +r %s* 2>/dev/null || true" % shlex.quote(ova + ".zst"))
    if qemu_args:
        cl = state(osname, "cmdline")
        if os.path.exists(cl):
            shutil.copy(cl, qemu_args)
        else:
            with open(qemu_args, "w") as f:
                f.write("# no launch descriptor recorded for %s\n" % osname)
        # Normalized guest-shape profile published beside the qcow2 so anyvm.py
        # launches from a single source of truth (see build_guest_profile()).
        # Best-effort: the image is already exported, so a profile failure must
        # never fail the build -- anyvm.py just falls back to its built-in logic
        # when the asset is missing.
        prof_path = re.sub(r"\.qemu$", "", qemu_args) + ".profile.json"
        try:
            profile = build_guest_profile()
            with open(prof_path, "w") as f:
                json.dump(profile, f, indent=2, sort_keys=True)
                f.write("\n")
            log("Wrote guest profile %s" % prof_path)
            _profile_sanity_check(profile, cl)
        except Exception as e:
            log("WARNING: could not write guest profile %s: %s" % (prof_path, e))
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
                               (osname, sshport, opts) as bare names.
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

# ============================================================================
# (H2) telnet transport -- VM_TRANSPORT=telnet
#
# For guests with no sshd at all (plan9/9front) the build drives the guest
# through its telnetd (baked to listen with no auth, reachable only via the
# slirp hostfwd on 127.0.0.1). Command lines are sent as-is; completion is
# judged by settle time and callers grep the returned transcript for their
# own markers. There is no exit-status channel -- a hook that needs one must
# have the guest echo a marker (rc: `echo done-$status`).
# ============================================================================

def _telnet_eat_iac(sock, data, out):
    """Consume telnet IAC negotiation in `data`, refusing every option, and
    append the plain bytes to `out`."""
    IAC, SE, SB = 255, 240, 250
    WILL, WONT, DO, DONT = 251, 252, 253, 254
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd in (DO, DONT, WILL, WONT) and i + 2 < n:
            opt = data[i + 2]
            try:
                if cmd == DO:
                    sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    sock.sendall(bytes([IAC, DONT, opt]))
            except OSError:
                pass
            i += 3
        elif cmd == SB:
            j = i + 2
            while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            i = j + 2
        elif cmd == IAC:
            out.append(IAC)
            i += 2
        else:
            i += 2


def telnet_exec(cmds, settle=2.0, port=None):
    """Run command lines in the guest over telnet. `cmds` is a list of
    command strings (sent one by one, each followed by CRLF, waiting
    `settle` seconds after each). Returns (connected, transcript_text):
    connected is False when the TCP connect failed or the peer closed
    mid-session; transcript_text is everything the guest printed."""
    osname = env("VM_OS_NAME") or ""
    if port is None:
        try:
            port = int(read_state(osname, "sshport") or "23")
        except ValueError:
            port = 23
    out = bytearray()
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), 10)
    except OSError:
        return False, ""
    alive = True

    def _read_for(seconds):
        end = time.time() + seconds
        sock.settimeout(0.5)
        while time.time() < end:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not data:
                return False
            _telnet_eat_iac(sock, data, out)
        return True

    alive = _read_for(min(settle, 2.0))
    for c in cmds:
        if not alive:
            break
        try:
            sock.sendall(c.encode("utf-8", "replace") + b"\r\n")
        except OSError:
            alive = False
            break
        alive = _read_for(settle)
    try:
        sock.close()
    except OSError:
        pass
    return alive, out.decode("utf-8", "replace")


def _telnet_ready_check():
    """One telnet probe: connect, run a marker echo, look for the marker in
    the output. The quoted split in the sent line keeps the guest's echo of
    the command itself from matching."""
    ok, text = telnet_exec(["echo anyvm''-ready"], settle=2.0)
    return ok and ("anyvm-ready" in text)


def _wait_telnet(max_retries=100, restart_cb=None):
    """Poll the guest's telnetd through the hostfwd port until it answers the
    marker probe; optional restart_cb runs once on terminal failure. Includes
    the same hostfwd-IP guard as _wait_ssh (stale DHCP lease -> rewrite the
    forward via the monitor)."""
    osname = env("VM_OS_NAME") or ""
    sshport_str = read_state(osname, "sshport") or "23"
    try:
        sshport = int(sshport_str)
    except ValueError:
        sshport = 23
    retry = 0
    restarted = False
    guard_done = False
    while True:
        if _telnet_ready_check():
            break
        log("telnet is not ready, just wait.")
        if not guard_done:
            actual = detect_guest_ip(osname)
            if actual:
                if actual == SLIRP_EXPECTED_GUEST_IP:
                    log("guest IP %s matches hostfwd target, ok" % actual)
                    guard_done = True
                else:
                    log("guest IP %s != expected %s; rewriting hostfwd via monitor"
                        % (actual, SLIRP_EXPECTED_GUEST_IP))
                    if rewrite_hostfwd_target(sshport, actual, guest_port=23):
                        log("hostfwd rewritten to %s:23" % actual)
                        guard_done = True
                    else:
                        log("hostfwd rewrite failed; will retry next iteration")
        time.sleep(10)
        retry += 1
        if retry > max_retries:
            if restarted or not restart_cb:
                log("telnet is failed."); return False
            log("telnet failed; trying restart")
            restarted = True
            restart_cb()
            retry = 0
            guard_done = False
    log("telnet is ready.")
    return True


def _ssh_ready_check(timeout=None):
    """Probe `ssh $VM_OS_NAME exit` with `-v` so the caller can inspect why
    the connection failed (auth refused, no route, perm denied, banner
    timeout). Returns (success, stderr_text). stderr_text is empty on success
    and on TimeoutExpired (the verbose dump up to the kill point isn't useful
    when ssh hung mid-handshake).

    Default 10 s -- short enough that retry polling stays snappy, long enough
    to cover SSH banner + key exchange + auth on slow guests (illumos sshd
    in particular takes 4-5 s for the full handshake even on a healthy KVM
    boot; an over-tight 2 s window misreads a working sshd as down). Override
    with $VM_SSH_READY_TIMEOUT when the guest needs longer -- e.g. Ubuntu
    24.04 under TCG aarch64 emulation, where systemd-socket-activated
    ssh@.service can take 30-60 s to spin up on the first connection, so a
    10 s window misreads every probe as "connection timeout"."""
    osname = env("VM_OS_NAME")
    if not osname:
        return False, ""
    if timeout is None:
        try:
            timeout = int(env("VM_SSH_READY_TIMEOUT") or "10")
        except (TypeError, ValueError):
            timeout = 10
    # -v gives the line we actually want to see ("Permission denied
    # (publickey)", "Connection refused", "ssh: connect to host ... port
    # 22: No route to host"). LogLevel=ERROR would mute -v -- drop it.
    cmd = ["ssh", "-v",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=%d" % max(1, int(timeout)),
           osname, "exit"]
    try:
        r = subprocess.run(cmd, stdout=DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, ""
    ok = (r.returncode == 0)
    err = "" if ok else (r.stderr or b"").decode("utf-8", "replace")
    return ok, err


def _ssh_verbose_summary(stderr_text, head=20, tail=10):
    """Trim ssh -v output to the lines most likely to identify the failure.

    Full -v dump per retry is too noisy (~50 lines * many retries = thousands
    of lines in CI logs). Show the connect / handshake header and the tail
    where the actual auth/refusal lines live."""
    if not stderr_text:
        return ""
    lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
    if len(lines) <= head + tail:
        return "\n".join(lines)
    return "\n".join(lines[:head] + ["    ... (%d lines trimmed) ..." % (len(lines) - head - tail)] + lines[-tail:])


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
    # Dump ssh -v output every Nth failed retry so we can see WHY ssh
    # failed (auth denied, refused, etc.) without flooding the build log.
    VERBOSE_EVERY = 5
    while True:
        ok, err = _ssh_ready_check()
        if ok:
            break
        # First failure and every Nth retry: log the trimmed -v output so
        # the failure reason is visible in CI logs.
        if retry == 0 or (retry % VERBOSE_EVERY) == 0:
            summary = _ssh_verbose_summary(err)
            if summary:
                log("ssh probe %d (-v summary):\n%s" % (retry, summary))
            else:
                log("ssh probe %d: connection timeout (no verbose output)" % retry)
        else:
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
    if not osname: return 1
    # A boot can die underneath us instead of reaching the login prompt --
    # a random guest kernel panic (seen: NetBSD 10.0 wm(4) PHY-tick panic
    # under KVM) or a cmd646-wedged crawl on sparc64 -- and an unbounded
    # waitForText then polls forever until a human cancels the CI job.
    # Bound each login wait (VM_LOGIN_MAX_SECONDS, default 600 s) and
    # reroll the boot (force-kill + relaunch) once before giving up:
    # panics and wedges are random, so a fresh boot usually clears them.
    # waitForText returns 0 on both match and timeout by design, so success
    # is judged by re-checking the current screen for the tag.
    lmax = int(env("VM_LOGIN_MAX_SECONDS") or 600)
    attempts = 2
    for attempt in range(1, attempts + 1):
        if startVM() != 0:
            log("start_and_wait: startVM failed for %s, aborting" % osname)
            return 1
        time.sleep(2); openConsole()
        if run_hook("waitForLoginTag"):
            time.sleep(3)
            return 0
        waitForText(env("VM_LOGIN_TAG"), str(lmax))
        if env("VM_LOGIN_TAG") in _screen_text_value(None):
            time.sleep(3)
            return 0
        log("start_and_wait: no login prompt within %d s (attempt %d/%d); "
            "force-killing for a fresh boot" % (lmax, attempt, attempts))
        closeConsole()
        destroyVM()
    log("start_and_wait: %s never reached the login prompt; aborting" % osname)
    return 1


def shutdown_and_wait():
    osname = _check_osname("shutdown_and_wait")
    if not osname: return
    if env("VM_TRANSPORT") == "telnet":
        # Deliver the shutdown command over telnet; the guest (plan9 fshalt)
        # halts the CPU without powering QEMU off, so the real wait below
        # relies on _wait_vm_down's halt-banner detection + force-kill.
        ok, _text = telnet_exec([env("VM_SHUTDOWN_CMD")], settle=10.0)
        log("telnet shutdown command sent (connected=%s)" % ok)
        time.sleep(10)
        if isRunning() == 0:
            if shutdownVM() != 0:
                log("shutdown error")
        smax = int(env("VM_SHUTDOWN_MAX_SECONDS") or 1800)
        _wait_vm_down(what="VM shutdown", poll=5, max_seconds=smax)
        closeConsole()
        return
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
           osname, env("VM_SHUTDOWN_CMD")]
    # The remote shutdown command kills the very connection it runs over, so
    # this ssh must NOT be awaited unbounded: when the guest tears down sshd
    # without closing the TCP session (seen on NetBSD/sparc64 when the cmd646
    # lost-interrupt state makes rc.shutdown crawl), the half-open session
    # hangs in slirp's hostfwd with no FIN/RST and the ssh client never exits
    # -- a CI job sat here for 4.5 h without ever reaching the bounded
    # poweroff wait below. The command itself is delivered and starts running
    # within seconds, so cap the ssh session; _wait_vm_down() does the real
    # waiting (VM_SHUTDOWN_MAX_SECONDS + force-kill).
    try:
        rc = subprocess.run(cmd, timeout=120).returncode
    except subprocess.TimeoutExpired:
        log("shutdown ssh still connected after 120 s; the command was "
            "delivered, proceeding to the poweroff wait")
        rc = 0
    if rc != 0:
        log("shutdown rc=%d, ignoring (haiku?)" % rc)
    time.sleep(30)
    if isRunning() == 0:
        if shutdownVM() != 0:
            log("shutdown error")
    # VM_SHUTDOWN_MAX_SECONDS lets a conf extend the poweroff wait. NetBSD
    # sparc64 needs this: concurrent net+disk DMA during the build (pkg_add)
    # drives QEMU's sun4u CMD646 into a sustained "lost interrupt" state, so the
    # final `shutdown -p` sync crawls (each command times out ~10s). Give it
    # enough time to reach "has halted" cleanly -- a premature force-kill mid-
    # sync can corrupt the root FFS and drop the verify VM to single-user.
    smax = int(env("VM_SHUTDOWN_MAX_SECONDS") or 1800)
    _wait_vm_down(what="VM shutdown", poll=5, max_seconds=smax)
    closeConsole()


def restart_and_wait():
    shutdown_and_wait(); return start_and_wait()


def _prep_vhd_disk(link):
    """Materialize $osname.qcow2 from a published cloud image URL."""
    osname = env("VM_OS_NAME")
    qcow = wf("%s.qcow2" % osname)
    if os.path.exists(qcow): return
    # download() aborts the build itself on an unrecoverable download; every
    # decompress / image-convert below uses must_run/must_sh so a failure there
    # also aborts (FATAL + exit 1) rather than silently leaving a corrupt qcow2
    # that only blows up much later at boot.
    if link.endswith("img.gz"):
        img = wf("%s.img" % osname)
        if not os.path.exists(img):
            try: os.remove(img + ".gz")
            except OSError: pass
            download(link, img + ".gz")
            must_sh("gunzip -c %s.gz > %s" % (shlex.quote(img), shlex.quote(img)),
                    "gunzip %s.gz (corrupt download?)" % img)
        must_run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
                  "-o", "preallocation=off", img, qcow], "qemu-img convert")
    elif link.endswith("img.zst"):
        img = wf("%s.img" % osname)
        if not os.path.exists(img):
            try: os.remove(img + ".zst")
            except OSError: pass
            download(link, img + ".zst")
            must_run(["zstd", "-f", "-d", img + ".zst", "-o", img], "zstd decompress")
        must_run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
                  "-o", "preallocation=off", img, qcow], "qemu-img convert")
    elif link.endswith("img.tar.gz") or link.endswith("img.tar.xz"):
        # Tarball holding a single raw *.img member whose name varies per
        # snapshot (e.g. Debian GNU/Hurd publishes
        # debian-hurd-amd64-20250807.img.tar.gz). Extract the member straight
        # to stdout (-O) so we never depend on the member's name and never
        # need a scratch directory; qemu-img convert re-sparsifies the zeros
        # the -O stream expands. The extracted .img is KEPT (same retry-cache
        # semantics as the img.gz branch): clearVM() deletes the qcow2 on
        # every run, so removing the .img would force a full re-download +
        # re-extract on each rebuild attempt.
        img = wf("%s.img" % osname)
        if not os.path.exists(img):
            tarball = wf("%s.imgtar" % osname)
            comp = "z" if link.endswith(".gz") else "J"
            tarcmd = ("tar -x%sf %s --wildcards -O '*.img' > %s"
                      % (comp, shlex.quote(tarball), shlex.quote(img)))
            # Reuse an existing tarball (a prior run's download survives a
            # clearVM). If it is absent or turns out corrupt, (re)download
            # once and extract again -- that second failure is fatal.
            if not os.path.exists(tarball) or sh(tarcmd) != 0:
                try: os.remove(tarball)
                except OSError: pass
                download(link, tarball)
                must_sh(tarcmd, "tar extract %s (corrupt download?)" % tarball)
        must_run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2",
                  "-o", "preallocation=off", img, qcow], "qemu-img convert")
    elif link.endswith(".img"):
        tmp = wf("%s.download.img" % osname)
        if not os.path.exists(tmp):
            download(link, tmp)
        must_run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off",
                  tmp, qcow], "qemu-img convert")
        try: os.remove(tmp)
        except OSError: pass
    elif link.endswith(".qcow2.gz"):
        # gzip-compressed qcow2 (9front publishes its prebuilt images this
        # way). gunzip to a temp qcow2, then the usual convert re-sparsifies.
        tmp = wf("%s.download.qcow2" % osname)
        if not os.path.exists(tmp):
            gz = tmp + ".gz"
            try: os.remove(gz)
            except OSError: pass
            download(link, gz)
            must_sh("gunzip -c %s > %s" % (shlex.quote(gz), shlex.quote(tmp)),
                    "gunzip %s (corrupt download?)" % gz)
            try: os.remove(gz)
            except OSError: pass
        must_run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off",
                  tmp, qcow], "qemu-img convert")
        try: os.remove(tmp)
        except OSError: pass
    elif link.endswith(".qcow2"):
        tmp = wf("%s.download.qcow2" % osname)
        if not os.path.exists(tmp):
            download(link, tmp)
        must_run(["qemu-img", "convert", "-O", "qcow2", "-o", "preallocation=off",
                  tmp, qcow], "qemu-img convert")
        try: os.remove(tmp)
        except OSError: pass
    else:
        xz = qcow + ".xz"
        if not os.path.exists(xz):
            download(link, xz)
        must_run(["xz", "-d", "-T", "0", "--verbose", xz], "xz decompress")


def _gen_enablessh_local():
    """Build enablessh.local: enablessh.txt + authorized_keys append (twice,
    once base64-roundtripped to dodge encoding bugs we've seen in console
    paste paths) + chmod.

    Final block enforces correct .ssh permissions in case the per-builder
    enablessh.txt clobbered them (e.g. a `chmod -R 600 ~/.ssh` line that
    leaves the *directory* at mode 600, which sshd-StrictModes refuses to
    traverse -- causing every pubkey login to bounce to PAM and burn auth
    attempts, ending in "maximum authentication attempts exceeded")."""
    idrsa = os.path.join(HOME, ".ssh", "id_rsa")
    if not os.path.exists(idrsa):
        run(["ssh-keygen", "-f", idrsa, "-q", "-N", ""])
    pub_path = idrsa + ".pub"
    pub = open(pub_path).read().rstrip("\n")

    try: os.remove(wf("enablessh.local"))
    except OSError: pass
    shutil.copy("enablessh.txt", wf("enablessh.local"))
    with open(wf("enablessh.local"), "a") as f:
        f.write("echo '%s' >>~/.ssh/authorized_keys\n\n\n\n" % pub)
        b64 = base64.b64encode(pub.encode("utf-8")).decode("ascii")
        f.write("echo '%s' | openssl base64 -d >>~/.ssh/authorized_keys\n\n\n"
                % b64)
        # The base64-roundtrip append above pipes through `openssl base64 -d`,
        # which does NOT emit a trailing newline, so authorized_keys ends
        # mid-line. Append one explicit newline right after the key writes so
        # the file is well-formed (and any key appended later can't concatenate
        # onto this one's line).
        f.write("echo >>~/.ssh/authorized_keys\n\n")
        # sshd StrictModes (default) requires .ssh dir 700 + authorized_keys
        # 600. Set both explicitly -- belt-and-suspenders against a buggy
        # `chmod -R 600` in the per-builder enablessh.txt.
        f.write("\nchmod 700 ~/.ssh\n")
        f.write("chmod 600 ~/.ssh/authorized_keys\n\n")
        # Force StrictModes off so any remaining permission anomaly (parent
        # dir mode, immutable flag, weird umask in the live image) can no
        # longer block pubkey auth. Idempotent: only appends when the line
        # isn't already there. The `service sshd restart` line from the
        # per-builder enablessh.txt picks the new config up.
        f.write("chmod u+w /etc/ssh/sshd_config 2>/dev/null || true\n")
        f.write("grep -q '^StrictModes no' /etc/ssh/sshd_config || "
                "echo 'StrictModes no' >> /etc/ssh/sshd_config\n")
        f.write("chmod u-w /etc/ssh/sshd_config 2>/dev/null || true\n\n\n")
    log(open(wf("enablessh.local")).read())


def _enable_ssh_root_branch(sshport):
    """The VM_USE_SSHROOT_BUILD_SSH path: sshpass into root@guest, feed
    enablessh.local; under slirp we connect via the hostfwd port on 127.0.0.1
    (the guest's 192.168.122.x is not host-reachable)."""
    vmip = getVMIP()
    log("guest slirp ip: %s (connecting via hostfwd 127.0.0.1:%s)" % (vmip, sshport))
    with open(wf("enablessh.local"), "rb") as inp:
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
    # Two early enters to flush whatever junk getty buffered during boot, then
    # a long pause for the kernel to drain those bytes and for getty to settle
    # at a clean "login:" prompt. Without this pause, login on slow consoles
    # (NetBSD evbarm plcom0) merges the trailing enters with the "root" that
    # follows and treats the whole thing as an empty username, so "root" never
    # echoes and never logs in.
    inputKeys("enter"); inputKeys("enter"); time.sleep(20)
    inputKeys("enter"); inputKeys("enter"); time.sleep(5)
    inputKeys("string root; enter")
    time.sleep(5)
    if env("VM_ROOT_PASSWORD"):
        inputKeys("string %s ; enter" % env("VM_ROOT_PASSWORD"))
        time.sleep(10)
    inputKeys("enter"); time.sleep(20); inputKeys("enter")
    screenText()
    if run_hook("enableNetwork"):
        screenText(); time.sleep(60)
    if env("VM_USE_NC_ENABLE_SSH"):
        inputFileNC(wf("enablessh.local"))
    elif env("VM_USE_BASH_ENABLE_SSH"):
        inputFileBash(wf("enablessh.local"))
    else:
        inputFile(wf("enablessh.local"))
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

    # Everything the build generates lives under WORKDIR (keeps the repo root
    # clean; see wf()). Create it before any hook / setup / QEMU launch writes
    # into it.
    os.makedirs(WORKDIR, exist_ok=True)

    # Expose pipeline globals to hooks (so hook code can use bare `osname`
    # etc., mirroring how build.sh's source-d hooks saw shell variables).
    g = globals()
    g["osname"] = env("VM_OS_NAME")
    g["sshport"] = env("VM_SSH_PORT")
    g["opts"] = env("VM_OPTS")
    osname = g["osname"]
    sshport = g["sshport"]
    opts = g["opts"]

    # Tell hooks where the generated files live. build.py routes the working
    # qcow2 (and everything else) under WORKDIR via wf(); host_* hooks that
    # operate on that image -- prepareImage / finalizeImage doing qemu-nbd /
    # virt-customize -- must target the SAME path, so export it. Hooks read
    # `$VM_WORK_QCOW` (fall back to `${VM_OS_NAME}.qcow2` for standalone runs).
    os.environ["VM_WORKDIR"] = WORKDIR
    os.environ["VM_WORK_QCOW"] = wf("%s.qcow2" % osname)

    # Earliest hook point: runs before setup() (which, among other things,
    # extracts VM_QEMU_TAR), so a builder can generate build inputs on the
    # fly -- e.g. ubuntu-builder's hooks/host_beforeBuild.sh compiles its
    # pinned QEMU tarball here instead of committing 30MB binaries to git.
    run_hook("beforeBuild")

    startWeb("needOCR")
    setup("needOCR")

    log("============== host CPU ==============")
    sh("lscpu || cat /proc/cpuinfo || true")
    log("=====================================")

    if clearVM() != 0:
        log("vm does not exist (ok)")

    if env("VM_ISO_LINK"):
        if createVM(env("VM_ISO_LINK"), sshport, env("VM_PRE_DISK_LINK")) != 0:
            log("createVM failed; aborting")
            return 1
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
        createVMFromVHD(sshport)
        time.sleep(5)
    else:
        log("no VM_ISO_LINK or VM_VHD_LINK, can not build.")
        return 1

    log("VM image size immediately after install:")
    sh("ls -lh")

    if not env("VM_NO_VNC_BUILD"):
        os.environ["VM_USE_CONSOLE_BUILD"] = ""

    if start_and_wait() != 0:
        log("first boot never reached the login prompt; aborting")
        return 1
    telnet_transport = (env("VM_TRANSPORT") == "telnet")
    if telnet_transport:
        # No sshd in the guest; the enablessh hook sets up the guest's own
        # remote-exec channel (plan9: telnetd + exportfs listeners) instead.
        # Still make sure the host keypair exists -- the exported sidecar
        # contract (<output>-id_rsa.pub / <output>-host.id_rsa) is kept
        # identical so anyvm.py's asset handling stays uniform.
        idrsa = os.path.join(HOME, ".ssh", "id_rsa")
        if not os.path.exists(idrsa):
            run(["ssh-keygen", "-f", idrsa, "-q", "-N", ""])
    else:
        _gen_enablessh_local()

    if not run_hook("enablessh"):
        if telnet_transport:
            log("VM_TRANSPORT=telnet but no enablessh hook set up the guest "
                "listeners; aborting")
            return 1
        if env("VM_USE_SSHROOT_BUILD_SSH"):
            _enable_ssh_root_branch(sshport)
        else:
            _enable_ssh_console_branch()

    def _restart():
        if isRunning() == 0 and shutdownVM() != 0:
            log("shutdown error"); sys.exit(1)
        _wait_vm_down(what="VM restart", poll=5)
        closeConsole(); start_and_wait()

    if telnet_transport:
        if not _wait_telnet(restart_cb=_restart):
            return 1
    else:
        addSSHHost()
        log("Sleep for the sshd to restart"); time.sleep(10)

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
        if restart_and_wait() != 0:
            log("post-build reboot never reached the login prompt; aborting")
            return 1
        if not _wait_ssh():
            log("ssh is failed."); return 1

    output = "%s-%s" % (osname, env("VM_RELEASE"))
    if env("VM_ARCH"):
        output = "%s-%s" % (output, env("VM_ARCH"))
    # Route every release-artifact file (<output>.qcow2.zst + -id_rsa.pub /
    # -host.id_rsa / .qemu / .profile.json sidecars) into WORKDIR by making the
    # shared basename a WORKDIR-relative path prefix. The CI upload step
    # (build.tpl.yml) reads them from build/ to match.
    output = wf(output)
    if telnet_transport:
        # No ssh in the guest to cat a key out of; publish the host's pubkey
        # so the sidecar asset set stays complete (unused at runtime).
        pub_src = os.path.join(HOME, ".ssh", "id_rsa.pub")
        with open("%s-id_rsa.pub" % output, "w") as f:
            if os.path.exists(pub_src):
                with open(pub_src) as src:
                    f.write(src.read())
    else:
        with open("%s-id_rsa.pub" % output, "w") as f:
            subprocess.run(["ssh", osname, "cat ~/.ssh/id_rsa.pub"], stdout=f)

    if env("VM_PRE_INSTALL_PKGS"):
        inst_script = env("VM_INSTALL_SCRIPT")
        if inst_script:
            # Run a conf-provided install script in the guest instead of the
            # plain "VM_INSTALL_CMD <pkgs>" one-liner. The local script file is
            # piped into the guest shell over ssh stdin (same transport as
            # VM_EXTRA_SCRIPT below) with two variables prepended:
            #   ANYVM_PKGS     - the VM_PRE_INSTALL_PKGS package list
            #   ANYVM_PKG_PATH - the conf's VM_PKG_PATH (e.g. a host-resolved
            #                    binary package repo URL), may be empty
            # First user: NetBSD/sparc64 two-phase pkg install (download the
            # whole dependency closure into a tmpfs first, then pkg_add from
            # RAM) to avoid the concurrent net+disk DMA that wedges QEMU
            # sun4u's CMD646 into its lost-interrupt state.
            log("install script: %s" % inst_script)
            with open(inst_script, "r") as f:
                inst_body = f.read()
            payload = 'set -e\nANYVM_PKGS="%s"\nANYVM_PKG_PATH="%s"\n%s\n' % (
                env("VM_PRE_INSTALL_PKGS"), env("VM_PKG_PATH") or "", inst_body)
        else:
            cmd = "%s %s" % (env("VM_INSTALL_CMD"), env("VM_PRE_INSTALL_PKGS"))
            log(cmd)
            payload = "set -e\n%s\n" % cmd
        # Relax the client keepalive for this session: ~/.ssh/config (the
        # enablessh block above) sets ServerAliveInterval=1, i.e. the client
        # drops the connection after ~3 s without a keepalive reply. A
        # CPU-heavy install step on a TCG guest (e.g. pkgfetch's gunzip+awk
        # over a 25k-entry pkg_summary on an emulated sparc64) starves sshd
        # long enough to trip that, the stdin-fed sh dies with the
        # connection, and the packages silently never install. Command-line
        # -o options override ssh_config, so tolerate ~10 minutes of
        # unresponsiveness here.
        #
        # One reroll on failure, like the login and verify waits: a guest can
        # die underneath the install for random reasons (a sparc64 cmd646
        # wedge cascading into disk EIO killed sshd mid-fetch on CI:
        # "Connection ... closed by remote host", rc=255). Reboot once and
        # retry; a deterministic failure (a package that genuinely cannot
        # install) still fails the build on the second attempt.
        inst_ok = False
        for inst_attempt in (1, 2):
            rc = subprocess.run(["ssh", "-o", "ServerAliveInterval=30",
                                 "-o", "ServerAliveCountMax=20",
                                 osname, "sh"], input=payload.encode()).returncode
            if rc == 0:
                inst_ok = True
                break
            log("install step FAILED rc=%d (attempt %d/2)" % (rc, inst_attempt))
            if inst_attempt == 1:
                log("rebooting the guest for a fresh install attempt")
                if restart_and_wait() != 0:
                    break
        if not inst_ok:
            # Fail the build: a green job that ships an artifact without its
            # packages is worse than a red one (Ubuntu cloud images dropped
            # their pre-baked universe indexes in the 2026-06-10 serials and
            # 12 jobs went green while every artifact was missing
            # rsync/sshfs/nfs-common -- caught only by the downstream anyvm
            # tests). A package that genuinely cannot install on some
            # release belongs OUT of that conf's VM_PRE_INSTALL_PKGS list,
            # not silently tolerated.
            return 1

    extra = env("VM_EXTRA_SCRIPT")
    if extra:
        log(extra)
        # Same relaxed keepalive as the install step above: a long CPU burst
        # in the guest must not get the stdin-fed script killed mid-run.
        #
        # CHECK THE EXIT CODE. The desktop hooks (xfce.sh/gnome.sh/kde6.sh,
        # openbsd vm_*.sh) run `set -e`, so a failed `pkg install` aborts them
        # non-zero. build.py used to ignore that rc and export the image
        # anyway: a transient "pkg: No packages available matching
        # 'plasma6-plasma'" left the FreeBSD 15.1-kde6 v2.1.8 artifact with NO
        # desktop, yet the build still went green and was published. Fail the
        # build on any non-zero rc so a desktop-less image is never shipped.
        # One failure is a failure -- no retry; rerun the job to recover from a
        # transient repo-catalogue hiccup.
        with open(extra, "rb") as f:
            rc = subprocess.run(["ssh", "-o", "SendEnv=VM_RELEASE",
                                 "-o", "ServerAliveInterval=30",
                                 "-o", "ServerAliveCountMax=20",
                                 osname, "sh"], stdin=f).returncode
        if rc != 0:
            log("VM_EXTRA_SCRIPT %s FAILED rc=%d; aborting (refusing to ship "
                "an image whose extra script did not complete)" % (extra, rc))
            return 1

    # finalize is the LAST in-guest hook point: it runs AFTER VM_EXTRA_SCRIPT
    # (it used to run just before it) so image-slimming cleanup in a
    # finalize hook -- dropping package caches, TRIM/zero-fill of freed
    # blocks so the export sparsify can reclaim them -- also covers the
    # desktop variants' package churn. The desktop scripts never reboot the
    # guest (their "reboot to apply" is the exported image's first boot), so
    # ssh is still up here. Best-effort by design: run_hook ignores the
    # guest rc, so a failed cleanup never sinks an otherwise-good build.
    run_hook("finalize")

    # Show authorized_keys and assert it ends with a trailing newline -- here,
    # on the live build VM, BEFORE shutdown/export, so it runs for EVERY image.
    # The post-export verification boot below is gated on VM_RSYNC_PKG /
    # VM_SSHFS_PKG (skipped for base-only images such as riscv64 / powerpc64),
    # so a check placed there never runs for them; this spot is ungated. The
    # build VM's disk IS the qcow2 about to be exported, so this is the shipped
    # content. Best-effort on ssh: if the VM answers, print authorized_keys and
    # fail the build when it does not end in a newline (_gen_enablessh_local's
    # base64 re-append emits none; the explicit `echo >>` is meant to terminate
    # it -- assert that held). If the VM is not ssh-reachable at this point
    # (some console-only images), warn and continue rather than fail.
    if telnet_transport:
        log("authorized_keys check: telnet transport, guest has no ssh; skipping")
    elif _ssh_ready_check()[0]:
        # Pull the whole authorized_keys back and judge it in Python rather
        # than with a remote shell test. `cat` runs under any login shell; a
        # POSIX `[ -z "$(...)" ]` does not -- FreeBSD roots default to tcsh,
        # which can't parse `$(...)` and bailed with "Illegal variable name.",
        # mis-failing a perfectly good file. The build VM's disk IS the qcow2
        # about to be exported, so this is the shipped content. cat returns
        # non-zero if the file is missing; empty stdout means an empty file;
        # otherwise the trailing-byte test is unambiguous here.
        r = subprocess.run(["ssh", osname, "cat ~/.ssh/authorized_keys"],
                           capture_output=True)
        ak = r.stdout
        log("======Show authorized_keys: ")
        log(ak.decode("utf-8", "replace").rstrip("\n"))
        if r.returncode != 0 or not ak or not ak.endswith(b"\n"):
            log("verification FAILED: ~/.ssh/authorized_keys is missing, empty, "
                "or has no trailing newline (rc=%d, %d bytes)"
                % (r.returncode, len(ak)))
            return 1
        log("verification OK: authorized_keys ends with a trailing newline")
    else:
        log("authorized_keys check: build VM not ssh-reachable here; skipping "
            "(not failing -- some console-only images have no ssh at this point)")

    shutdown_and_wait()

    # Host-side image-finalize hook (runs AFTER guest is down, BEFORE ISO is
    # removed below -- e.g. mounts the qcow2 to tweak files).
    run_hook("finalizeImage")

    if env("VM_ISO_LINK"):
        log("Clean up ISO for more space")
        try: os.remove(wf("%s.iso" % osname))
        except OSError: pass

    log("contents of home directory:"); sh("ls -lah")
    log("free space:"); sh("df -h")

    ova = "%s.qcow2" % output
    qemu_args = "%s.qemu" % output
    log("Exporting %s" % ova)
    exportOVA(ova, qemu_args)

    shutil.copy(os.path.join(HOME, ".ssh", "id_rsa"), "%s-host.id_rsa" % output)
    log("contents after export:"); sh("ls -lah")

    log("Checking the packages: %s %s" % (env("VM_RSYNC_PKG"), env("VM_SSHFS_PKG")))
    if not (env("VM_RSYNC_PKG") or env("VM_SSHFS_PKG")):
        log("skip")
    else:
        addSSHAuthorizedKeys("%s-id_rsa.pub" % output)
        # Bound this wait: the verification boot can wedge (sparc64 cmd646
        # crawl) or panic like any other boot, and the old `while True`
        # spun "not ready yet" forever -- a CI job sat 70+ minutes until a
        # human cancelled it. Give each boot VM_VERIFY_SSH_MAX_SECONDS
        # (default 600 s), then reroll once with a fresh QEMU before
        # giving up.
        vmax = int(env("VM_VERIFY_SSH_MAX_SECONDS") or 600)
        verify_ready = False
        for vattempt in (1, 2):
            if startVM() != 0:
                log("verification startVM failed; aborting")
                return 1
            vstart = time.time()
            vdeadline = vstart + vmax
            while time.time() < vdeadline:
                ok, _err = _ssh_ready_check()
                if ok:
                    verify_ready = True
                    break
                # Echo what the guest console is doing, same format as
                # _wait_vm_down. Without this the CI log is just a wall of
                # "not ready yet" and a failed verify boot is undiagnosable
                # (seen on a flaky 15.0-kde6 run: 2x600s of silence, no way
                # to tell bootloader hang from fsck from rc stall).
                vsize, vtail = _serial_tail_line()
                vmm, vss = divmod(int(time.time() - vstart), 60)
                log("[%dm%02ds] verify boot %d/2, serial=%dB | %s"
                    % (vmm, vss, vattempt, vsize, vtail[:140]))
                time.sleep(5)
            if verify_ready:
                break
            log("verification VM not ssh-reachable within %d s "
                "(attempt %d/2); force-killing for a fresh boot"
                % (vmax, vattempt))
            destroyVM()
        if not verify_ready:
            log("verification VM never became ssh-reachable; aborting")
            return 1
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

        # Tear down the verification VM so its QEMU process doesn't outlive
        # this build. Otherwise its hostfwd holds VM_SSH_PORT and the next
        # build in the same workspace (run locally, or any CI runner reused
        # by a follow-up matrix job) errors at QEMU launch with
        #   Could not set up host forwarding rule 'tcp:127.0.0.1:N-...'.
        # Use destroyVM() (SIGTERM -> SIGKILL the QEMU pid), NOT
        # shutdownVM()+_wait_vm_down: shutdownVM sends HMP system_powerdown
        # which is an ACPI shutdown request, and some guests ignore it (seen
        # on FreeBSD 13.5 aarch64 -- the verification VM stayed at the
        # login: prompt and _wait_vm_down looped for the entire 6h CI
        # budget). We're done with the qcow2 -- it was already exported --
        # so a hard kill is safe.
        if isRunning() == 0:
            destroyVM()
        closeConsole()

    log("Build finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
