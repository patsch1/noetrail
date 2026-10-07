#!/usr/bin/env python3
"""Compatibility shim for deployments that invoke this path directly.

Removal target: Noetrail 0.11.0. Use the ``noetrail`` console command or
``python3 -m noetrail.cli`` instead.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from noetrail.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
