#!/bin/sh
set -eu
if [ "$(id -u)" = "0" ]; then
    exec gosu scarguard "$@"
fi
exec "$@"
