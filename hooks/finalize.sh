


sed 's/^PASSREQ=YES/PASSREQ=NO/' /etc/default/login > /tmp/login.new
cat /tmp/login.new >/etc/default/login
passwd -d root
rm -f /tmp/login.new


cat /etc/resolv.conf




