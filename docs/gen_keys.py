#!/usr/bin/env python3
"""Regenerate the command-line and hot-key tables in docs/user-guide.md from helper.py,
so the in-app help (H) and the manual never drift apart.

    python docs/gen_keys.py        (run from the repository root with the venv active)
"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from helper import ARG_DESCRIPTIONS, CONTROL_DESCRIPTIONS   # noqa: E402

def table(rows, h1, h2):
    out = [f"| {h1} | {h2} |", "|---|---|"]
    for k, v in rows.items():
        out.append(f"| `{k}` | {v} |")
    return "\n".join(out)

def replace_block(text, name, body):
    start, end = f"<!-- generated:{name} -->", f"<!-- /generated:{name} -->"
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pat.search(text):
        sys.exit(f"markers for '{name}' not found in user-guide.md")
    return pat.sub(f"{start}\n{body}\n{end}", text)

path = os.path.join(ROOT, "docs", "user-guide.md")
text = open(path, encoding="utf-8").read()
keys = {k: v for k, v in CONTROL_DESCRIPTIONS.items() if not k.startswith("Mouse")}
mouse = {k.replace("Mouse ", ""): v for k, v in CONTROL_DESCRIPTIONS.items() if k.startswith("Mouse")}
text = replace_block(text, "args", table(ARG_DESCRIPTIONS, "Option", "Meaning"))
text = replace_block(text, "keys", table(keys, "Key", "Action"))
text = replace_block(text, "mouse", table(mouse, "Mouse", "Action"))
open(path, "w", encoding="utf-8").write(text)
print("docs/user-guide.md: tables regenerated from helper.py")
