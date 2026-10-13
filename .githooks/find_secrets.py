#!/usr/bin/env python3
"""Find real-looking passwords and stream keys, with the patterns Nova masks its logs with (util.py).

  git diff --cached -U0 | find_secrets.py     the pre-commit hook: added lines only
  find_secrets.py --tracked                    CI: every tracked file
Placeholders are fine: password pass pw pwd secret xxxx *** PASSWORD KEY YOUR-KEY YOUR-STREAM-KEY
$VAR ${VAR} %VAR%. tests/ and .githooks/ use synthetic credentials on purpose and are skipped."""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import util   # noqa: E402

PLACEHOLDER = re.compile(r"(?i)(password|pass|pw|pwd|secret|key|your-key|your-stream-key|"
                         r"[a-z_-]*x{3,}[x-]*|\**|\$\{?\w+\}?|%\w+%|<[^>]*>|\{\w*\}|\.\.\.|…)")
SKIP = ("tests/", ".githooks/", "util.py")      # synthetic credentials, and the patterns themselves
# the dict/argv forms only matter in log lines, where option names follow option names
PATTERNS = [p for p in util._SECRET_PATTERNS if p not in (util._RE_DICT_PWD, util._RE_OPT_PWD)]


def real(secret, pattern):
    s = secret.strip("`'\".,;:)]}…")
    if not s or PLACEHOLDER.fullmatch(s) or PLACEHOLDER.fullmatch(secret):
        return False
    if pattern is util._RE_RTMP_KEY:               # any rtmp path: docs say rtmp://host/live/cam1
        return len(s) >= 8 and any(c.isdigit() for c in s)
    return True


def find(text):
    return [m.group(2) for p in PATTERNS for m in p.finditer(text) if m.group(2) and real(m.group(2), p)]


def scan(lines):
    """lines: (file, line_no, text) -> list of findings, secrets masked in the report."""
    found = []
    for path, no, text in lines:
        if path.startswith(SKIP):
            continue
        hits = find(text)
        if hits:
            found.append(f"{path}:{no}: {len(hits)} secret(s), e.g. {hits[0][:2]}***")
    return found


def from_diff(stream):
    path, no = None, 0
    for line in stream:
        if line.startswith("+++ "):
            path = line[6:].rstrip("\n").rstrip("\t") if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            no = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            yield path, no, line[1:]
            no += 1


def tracked():
    files = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"], capture_output=True, text=True).stdout.split("\0")
    for f in filter(None, files):
        try:
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                for no, text in enumerate(fh, 1):
                    yield f, no, text
        except (UnicodeDecodeError, OSError):
            continue                                   # images and other binaries


if __name__ == "__main__":
    found = scan(tracked() if "--tracked" in sys.argv else from_diff(sys.stdin))
    if found:
        print("credentials or stream keys found:", *found, sep="\n  ", file=sys.stderr)
        print("Put real URLs in cameras.ini (git-ignored); committed files use placeholders.", file=sys.stderr)
        sys.exit(1)
