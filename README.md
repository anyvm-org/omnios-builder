

[![Build](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml/badge.svg)](https://github.com/anyvm-org/omnios-builder/actions/workflows/build.yml)

Latest: v2.0.6


The image builder for `omnios`


All the supported releases are here:



| Release | x86_64  |
|---------|---------|
| r151056 |  ✅     |
| r151054 |  ✅     |
| r151052 |  ✅     |
| r151050 |  ✅     |
| r151048 |  ✅     |
| r151046 |  ✅     |








How to build:

1. Use the [manual.yml](.github/workflows/manual.yml) to build manually.
   
    Run the workflow manually, you will get a view-only webconsole from the output of the workflow, just open the link in your web browser.
   
    You will also get an interactive VNC connection port from the output, you can connect to the vm by any vnc client.

2. Run the builder locally on your Ubuntu machine.

    Just clone the repo. and run:
    ```bash
    bash build.sh conf/omnios-r151056.conf
    ```
   
