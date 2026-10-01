"""PyInstaller entry point for panini-cli.exe."""

from panini_cli.agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
