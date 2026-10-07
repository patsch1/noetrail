"""Test package bootstrap.

Makes the ``src`` layout importable for the test process and for every CLI or
MCP subprocess it starts, and fails loudly on interpreters the project does not
support instead of silently collecting a smaller suite.

Discovery has to run with the repository root as the top-level directory
(``discover -s tests -t .``) so that this module is imported before any test
module, whatever order the individual modules import ``noetrail`` in.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile

if sys.version_info < (3, 11):  # noqa: UP036 - guard runs on unsupported runtimes
    raise RuntimeError(
        "Noetrail requires Python 3.11 or newer; "
        f"this interpreter is {sys.version_info.major}.{sys.version_info.minor}"
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPOSITORY_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

_inherited = os.environ.get("PYTHONPATH")
if not _inherited:
    os.environ["PYTHONPATH"] = str(SRC_ROOT)
elif str(SRC_ROOT) not in _inherited.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), _inherited])

CLI_COMMAND = [sys.executable, "-m", "noetrail.cli"]
MCP_COMMAND = [sys.executable, "-m", "noetrail.mcp"]
FETCHER_COMMAND = [sys.executable, "-m", "noetrail.bookmark_fetcher"]


def temporary_root(directory: tempfile.TemporaryDirectory[str] | str) -> Path:
    """The real path of a temporary directory used as a Noetrail root.

    ``tempfile`` returns the path as ``TMPDIR`` spells it. On macOS that is
    ``/var/folders/...``, while the same directory resolves to
    ``/private/var/folders/...`` because ``/var`` is a symlink. Every root
    Noetrail is given is resolved by ``resolve_layout``, so a test that keeps
    the unresolved spelling and compares it against a path the program reports
    is comparing two names for one directory.

    Nothing is wrong with the program: a vault behind a symlink works, because
    the write and the read resolve alike. The failure is confined to the tests,
    and only on a platform whose temporary directory sits behind a symlink —
    which is why CI on Linux never showed it and a maintainer running
    ``make check`` on macOS always did.

    Resolving here is a no-op wherever the two spellings already coincide.
    """

    name = (
        directory.name
        if isinstance(directory, tempfile.TemporaryDirectory)
        else directory
    )
    return Path(name).resolve()
