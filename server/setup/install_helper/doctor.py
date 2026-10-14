"""Compatibility entry point for host diagnostics."""

from server.operations.doctor import *  # noqa: F401,F403
from server.operations.doctor import main


if __name__ == "__main__":
    raise SystemExit(main())
