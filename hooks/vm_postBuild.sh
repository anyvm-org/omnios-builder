#some tasks run in the VM as soon as the vm is up






echo '=================== start ===='









bootadm set-menu timeout=1


svcadm disable sendmail

cat /etc/auto_master | grep -v /home >auto.txt
cat auto.txt >/etc/auto_master
rm -f auto.txt

automount -v
svcadm restart autofs


rm -f /etc/resolv.conf
echo "nameserver 8.8.8.8" >> /etc/resolv.conf
echo "nameserver 1.1.1.1" >> /etc/resolv.conf
echo "nameserver 9.9.9.9" >> /etc/resolv.conf


sed -i 's/^PARAM_REQUEST_LIST=\(.*\),6\(,.*\)$/PARAM_REQUEST_LIST=\1\2/; s/^PARAM_REQUEST_LIST=6,\(.*\)$/PARAM_REQUEST_LIST=\1/; s/^PARAM_REQUEST_LIST=\(.*\),6$/PARAM_REQUEST_LIST=\1/' /etc/default/dhcpagent

sed -i 's/^PARAM_IGNORE_LIST=$/PARAM_IGNORE_LIST=6/' /etc/default/dhcpagent


# anyvm: bring up networking independent of the NIC's PCI path / illumos
# instance number. The image is built on one VM (the NIC lands at one PCI slot
# -> e1000g0, pinned by device path in /etc/path_to_inst) but anyvm.py runs it
# on another where the NIC can land at a different slot -> e1000g1. The
# build-time static config pins e1000g0, which then cannot attach at runtime
# (DL_ATTACH_REQ DL_SYSERR errno 22) -> no DHCP -> no SSH. This boot script
# ignores the baked instance name and DHCP-plumbs whichever physical link is
# actually present and up, so the image works wherever the NIC enumerates.
# sshd listens on the wildcard 0.0.0.0:22, so it is reachable as soon as the
# lease lands even though this runs late (S99). The stale e1000g0 attach error
# is harmless: the system still reaches the multi-user milestone (which is what
# runs this script).
cat >/etc/rc2.d/S99anyvmnet <<'ANYVMNET'
#!/sbin/sh
PATH=/usr/sbin:/usr/bin:/sbin; export PATH

# Wait briefly for a physical link to report "up" (e1000 link negotiation),
# then DHCP-plumb the first such link. Fall back to the first physical link.
i=0
nic=""
while [ "$i" -lt 30 ]; do
    nic=`dladm show-phys -p -o link,state 2>/dev/null | awk -F: '$2 == "up" { print $1; exit }'`
    [ -n "$nic" ] && break
    i=`expr $i + 1`
    sleep 1
done
[ -z "$nic" ] && nic=`dladm show-phys -p -o link 2>/dev/null | head -1`
[ -z "$nic" ] && exit 0

ipadm show-if "$nic" >/dev/null 2>&1 || ipadm create-if "$nic"
ipadm show-addr "$nic/v4" >/dev/null 2>&1 || ipadm create-addr -T dhcp "$nic/v4"
exit 0
ANYVMNET
chmod 755 /etc/rc2.d/S99anyvmnet

