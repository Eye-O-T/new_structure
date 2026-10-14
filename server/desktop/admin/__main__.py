"""Run the administrator desktop independently from the installer."""

from .application import main


if __name__ == "__main__":
    raise SystemExit(main())
