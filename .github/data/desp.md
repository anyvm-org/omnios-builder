How the images are built:

Each image is built automatically in the
[anyvm-org/omnios-builder](https://github.com/anyvm-org/omnios-builder)
repo's GitHub Actions: it downloads the official OmniOS installer ISO,
boots it in QEMU, drives the installer unattended, enables ssh,
pre-installs the packages listed in the conf, and exports the installed
disk as a compressed qcow2 image.

Upstream install media: the official OmniOS ISOs from
https://downloads.omnios.org/media/ (download page:
https://omnios.org/download.html).
