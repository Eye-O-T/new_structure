"""Compatibility entry point for the relocated host CLI."""

from server.cli import *  # noqa: F401,F403
from server.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
