"""Developer wrapper; the same checks are embedded in panini-cli.exe sandbox uat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panini_cli.agent.sandbox_uat import main  # noqa: E402

if __name__ == "__main__":
    main()
