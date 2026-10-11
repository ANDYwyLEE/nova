#!/bin/sh
# Stop the cameras started by startall.sh (Ctrl-C semantics: geometry and recordings are saved).
cd "$(dirname "$0")" || exit 1
[ -f venv/bin/activate ] && . ./venv/bin/activate
exec python cams.py stop "$@"
