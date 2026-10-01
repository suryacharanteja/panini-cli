"""Source-tree entry point: `python run.py ...` behaves like the installed `panini-cli`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from panini_cli.agent.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
