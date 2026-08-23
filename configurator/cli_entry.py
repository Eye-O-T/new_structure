"""Console entry point used by the frozen Windows CLI executable."""

from configurator.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
