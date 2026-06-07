# Bring up the e1000 NIC + DHCP on OmniOS guest BEFORE inputFile pipes
# enablessh.local over nc.
#
# Why this exists:
#   On GHA the OmniOS install ISO's post-install main menu (Welcome / Shell /
#   Reboot / Halt) does not respond to our processOpts keystrokes -- the
#   pipeline ends up power-cycling the VM via HMP, and the disk-booted system
#   reaches the console login WITHOUT having gone through first-boot sysconfig
#   (or having had any NIC plumbed). With no IP, `nc 192.168.122.1 64342`
#   inside the guest hangs forever, so the enablessh.local payload that
#   _enable_ssh_console_branch tries to pipe over nc never executes, sshd
#   stays disabled, and the build.py SSH retry loop eventually gives up.
#
# Fix: after console root login, explicitly plumb e1000g0 and request a DHCP
# lease. Once that's up, the slirp host (192.168.122.1) is reachable and
# inputFile's nc pipe lands the enablessh.local payload in a root shell.
#
# Host-side hook: run by base-builder/build.py via exec() in this module's
# globals -- string / enter / time / log are bare names.

log("enableNetwork: plumbing e1000g0 and requesting DHCP")

string("ipadm create-if e1000g0")
enter()
time.sleep(2)

string("ipadm create-addr -T dhcp e1000g0/v4")
enter()
time.sleep(15)  # let dhcpagent grab a lease before we try nc

# Cosmetic: print the new IP so the build log shows the lease succeeded.
string("ipadm show-addr e1000g0/v4")
enter()
time.sleep(2)
