#!/bin/sh
# Start every enabled camera from cameras.ini in the background:  ./startall.sh [ID ...] [nova.py args]
cd "$(dirname "$0")" || exit 1
[ -f venv/bin/activate ] && . ./venv/bin/activate
exec python cams.py start "$@"
