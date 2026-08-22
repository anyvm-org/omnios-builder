

[![Build](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml/badge.svg)](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml)

Latest: v2.1.3


The image builder for `omnios`


All the supported releases are here:



| Release | Comments | LTS | End-of-Life | x86_64 |
|---------|---------|---------|---------|---------|
| r151058-build | build-essential | — | 2027-05-03 | ✅ (rsync,scp,nfs,tar) |
| r151058 | — | — | 2027-05-03 | ✅ (rsync,scp,nfs,tar) |
| r151056-build | build-essential | — | 2026-11-02 | ✅ (rsync,scp,nfs,tar) |
| r151056 | — | — | 2026-11-02 | ✅ (rsync,scp,nfs,tar) |
| r151054-build | build-essential | ✅ | 2028-05-01 | ✅ (rsync,scp,nfs,tar) |
| r151054 | — | ✅ | 2028-05-01 | ✅ (rsync,scp,nfs,tar) |
| r151052 | — | — | 2025-11-03 | ✅ (rsync,scp,nfs,tar) |
| r151050 | — | — | 2025-05-05 | ✅ (rsync,scp,nfs,tar) |
| r151048 | — | — | 2024-11-04 | ✅ (rsync,scp,nfs,tar) |
| r151046 | — | ✅ | 2026-05-01 | ✅ (rsync,scp,nfs,tar) |

<!-- extra-column: Comments -->
<!-- extra-value: r151058-build build-essential -->
<!-- extra-value: r151056-build build-essential -->
<!-- extra-value: r151054-build build-essential -->
<!-- extra-column: LTS -->
<!-- extra-value: r151054 ✅ -->
<!-- extra-value: r151054-build ✅ -->
<!-- r151046 LTS + EOL verified against omnios.org/schedule and omnios.org/releasenotes, 2026-07-26 -->
<!-- extra-value: r151046 ✅ -->
<!-- extra-column: End-of-Life -->
<!-- extra-value: r151058 2027-05-03 -->
<!-- extra-value: r151058-build 2027-05-03 -->
<!-- extra-value: r151056 2026-11-02 -->
<!-- extra-value: r151056-build 2026-11-02 -->
<!-- extra-value: r151054 2028-05-01 -->
<!-- extra-value: r151054-build 2028-05-01 -->
<!-- extra-value: r151052 2025-11-03 -->
<!-- extra-value: r151050 2025-05-05 -->
<!-- extra-value: r151048 2024-11-04 -->
<!-- extra-value: r151046 2026-05-01 -->

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




How to build:

1. Use the [manual.yml](.github/workflows/manual.yml) to build manually.
   
    Run the workflow manually, you will get a view-only webconsole from the output of the workflow, just open the link in your web browser.
   
    You will also get an interactive VNC connection port from the output, you can connect to the vm by any vnc client.

2. Run the builder locally on your Ubuntu machine.

    Just clone the repo. and run:
    ```bash
    python3 build.py conf/omnios-r151058.conf
    ```
   
