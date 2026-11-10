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


def test_the_overlay_names_the_command_key_on_a_mac():
    """On macOS Qt reports ⌘ as its Control modifier, and Control+Arrow never reaches the window (Mission Control,
    Spaces): the overlay said "Ctrl + Arrow", which does nothing there. The manual's table keeps Ctrl, with a note."""
    import helper
    mac = helper.control_descriptions(mac=True)
    assert "Cmd + Arrow" in mac and "Cmd + Mouse Drag" in mac and not any(k.startswith("Ctrl +") for k in mac)
    assert "(Cmd: enlarge the window" in mac["I / + / ="] and "(Cmd: shrink the window" in mac["O / - / _"]
    assert "Ctrl-C" in mac and len(mac) == len(helper.CONTROL_DESCRIPTIONS)
    assert helper.control_descriptions(mac=False) is helper.CONTROL_DESCRIPTIONS
    text = helper.generate_help_text({"-x": "y"}, mac)
    assert "Cmd + Arrow" in text and "Ctrl + Arrow" not in text
    guide = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "user-guide.md"), encoding="utf-8").read()
    assert "On a Mac, **Ctrl** in this table and the next is the Command key" in guide

