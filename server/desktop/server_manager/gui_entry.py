"""PyInstaller entry point for the server manager."""

from server.desktop.server_manager.application import main


if __name__ == "__main__":
    raise SystemExit(main())
