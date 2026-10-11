"""Developer wrapper; the same web UAT is embedded in panini-cli.exe sandbox uat --web."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from panini_cli.agent.web_uat import main  # noqa: E402

if __name__ == "__main__":
    main()
