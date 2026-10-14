"""Compatibility package for the relocated administrator desktop.

Submodules are resolved lazily from the new package directory so importing a
non-Qt helper does not load the complete desktop application.
"""

from pathlib import Path


__path__ = [
    str(Path(__file__).resolve().parents[3] / "desktop" / "admin")
]
