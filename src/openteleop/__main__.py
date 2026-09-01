"""Allow ``python -m openteleop`` to run the CLI."""
from openteleop.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
