# Hosted container test tooling

FDY-0574 correction (`container-test-ensurepip-127`): the web, notifier,
deterrent, backup and log-streamer test steps in `.github/workflows/build.yml`
create temporary `-runner` images on top of the runtime images. Bootstrap
these with `python3 -m ensurepip --upgrade`, then install the existing test
packages with `python3 -m pip install`. `ensurepip` need not create an
unversioned `pip` executable, so invoking bare `pip` can fail with exit 127.

This checkout contains five affected workflow commands and no Dockerfile
test stages using ensurepip. The correction changes only those five commands;
the runtime Dockerfiles, dependency locks, Jetson stack and self-hosted jobs
are unchanged. Test tooling remains in the disposable test images.

Local verification runs the actual generated Dockerfile RUN commands in an
isolated Python environment without an unversioned pip executable, including
repeated bootstrap with pip already installed. Service tests run in separate
processes. This does not substitute for hosted container builds and scans:
Docker is unavailable in the worker, and ARM/GPU checks are not exercised.
The final check results and independent review are recorded in the task report.

Do not merge until the release-only Orin CI prerequisite from FDY-0554/PR #223
is in the combined reviewed head and Foundry has reviewed the result. This
correction does not authorize a workflow dispatch, GPU job or release.
