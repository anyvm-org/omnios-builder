

[![Build](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml/badge.svg)](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml)

Latest: v2.1.0


The image builder for `omnios`


All the supported releases are here:



| Release       | x86_64 | Comments        | LTS | End-of-Life |
| ------------- | ------ | --------------- | --- | -------------- |
| r151058       | ✅     |                 |     | 2027-05-03     |
| r151058-build | ✅     | build-essential |     | 2027-05-03     |
| r151056       | ✅     |                 |     | 2026-11-02     |
| r151056-build | ✅     | build-essential |     | 2026-11-02     |
| r151054       | ✅     |                 | ✅  | 2028-05-01     |
| r151054-build | ✅     | build-essential | ✅  | 2028-05-01     |
| r151052       | ✅     |                 |     | 2025-11-03     |
| r151050       | ✅     |                 |     | 2025-05-05     |
| r151048       | ✅     |                 |     | 2024-11-04     |











How to build:

1. Use the [manual.yml](.github/workflows/manual.yml) to build manually.
   
    Run the workflow manually, you will get a view-only webconsole from the output of the workflow, just open the link in your web browser.
   
    You will also get an interactive VNC connection port from the output, you can connect to the vm by any vnc client.

2. Run the builder locally on your Ubuntu machine.

    Just clone the repo. and run:
    ```bash
    python3 build.py conf/omnios-r151058.conf
    ```
   
