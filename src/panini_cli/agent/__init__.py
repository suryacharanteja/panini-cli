"""PANINI-CLI: PANINI's own CLI agent, terminal CLI and app-server.

The agent works in one workspace through a small set of tools. Reads stay inside
the workspace and never touch secret files; every file change and every command
needs the user's approval; commands run in a killable process tree without the
host's credentials. PANINI drives it over the same app-server protocol it uses for
any compatible coding runtime.
"""

VERSION = "2.1.1b1"
