#!/bin/sh
# Run one camera in the foreground:  ./s.sh [ID] [nova.py args]     e.g.  ./s.sh frontdoor -r
# Cameras and their passwords live in cameras.ini (git-ignored); see cameras.example.ini.
cd "$(dirname "$0")" || exit 1
[ -f venv/bin/activate ] && . ./venv/bin/activate
exec python cams.py run "$@"
