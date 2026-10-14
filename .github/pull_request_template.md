## What and why

<!-- One paragraph: the problem, the change, how you verified it (which camera, OS, GPU). -->

## Checklist

- [ ] `pytest` passes (`pip install -r requirements-dev.txt`)
- [ ] The manual follows: `python docs/gen_keys.py` after changes to options or keys; the relevant `docs/*.md` page for behaviour changes
- [ ] A line in `CHANGELOG.md`
- [ ] No password, stream key or `cameras.ini` in the diff (`git config core.hooksPath .githooks` makes the pre-commit guard check for you)
