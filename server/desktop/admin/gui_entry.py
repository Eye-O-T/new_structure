"""PyInstaller entry point for the administrator desktop."""

from server.desktop.admin.application import main


if __name__ == "__main__":
    raise SystemExit(main())
