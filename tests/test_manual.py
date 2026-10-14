"""The manual lists every option, key, mouse action and environment variable the code has."""
import os
import re
import glob

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = open(os.path.join(ROOT, "docs", "user-guide.md"), encoding="utf-8").read()


def test_every_command_line_option_is_in_the_manual_with_its_help_text():
    import novaargs
    table = novaargs.option_table()
    assert "--camera" in " ".join(table) and "--control" not in " ".join(table)      # public ones only
    for flags, text in table.items():
        assert f"`{flags}`" in GUIDE, flags
        assert text.split(" Default:")[0] in GUIDE, flags


def test_every_environment_variable_the_code_reads_is_in_the_manual():
    used = set()
    for f in glob.glob(os.path.join(ROOT, "*.py")):
        used |= set(re.findall(r"NOVA_[A-Z_]+", open(f, encoding="utf-8").read()))
    documented = set(re.findall(r"`(NOVA_[A-Z_]+)`", GUIDE))
    assert used <= documented, used - documented


def test_every_launcher_command_is_in_the_manual():
    import cams
    verbs = re.findall(r"^  nova (\S+(?: \S+)?)", cams.USAGE, re.M)
    for v in verbs:
        head = v.split()[0] if v.split()[0] != "layout" else v
        assert f"nova {head}" in GUIDE, v
