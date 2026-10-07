# Synthetic demo

This directory is a completely synthetic Noetrail instance. It contains no
personal data and is safe to use in tests, screenshots, documentation, and
release artifacts.

- `data/` is the private-data-root shape used only for the demo.
- `config/` contains one local declarative schema pack.
- `data/imports/raw/markdown/` contains a generic Markdown import sample.

Validate it from the repository root:

```sh
noetrail \
  --data-root demo/data \
  --config-root demo/config \
  --builtins-root .knowledge \
  validate
```

For a clean installation walkthrough that creates a disposable copy, follow
the [five-minute quickstart](../docs/quickstart.md).

## Recorded session

`session.sh` is one agent turn — a search, a capture, the review queue, a
validation — run against a temporary copy of this instance, so the files here
are never modified. It prints each command before running it, and the printed
form is derived from the argument vector that actually executed.

`python3 ../tools/render_terminal_svg.py` runs the script and writes its output
to [`docs/assets/demo.txt`](../docs/assets/demo.txt) and
[`docs/assets/demo.svg`](../docs/assets/demo.svg), which is the image in the
README. Everything in that image is real program output; the only thing the
renderer changes is wrapping lines at 100 columns, the way a terminal does.

`tests/test_demo_recording.py` runs the session again and fails if the
committed recording no longer matches, ignoring only the entry ID, revision,
and timestamp that a fresh capture necessarily produces.
