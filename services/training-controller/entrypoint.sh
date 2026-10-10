#!/bin/sh
# v1.15 (FDY-0558): Entrypoint for the training-controller container.
#
# Runs as root to chown the /state volume (which on upgrade may carry
# root ownership from a prior controller installation), then drops to
# the scarguard user and exec's the real entrypoint.

set -e

# Migrate /state ownership so the non-root controller can write there.
if [ -d /state ]; then
    chown 999:999 /state
fi

# Drop to non-root and exec the controller.
exec gosu scarguard "$@"
