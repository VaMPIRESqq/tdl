"""Entry point for the terminal-only tdl binary.

Forced CLI/TUI dispatch: importing the GUI module (and therefore PySide6)
must never happen in this build, even when the user passes no arguments
(the default GUI route from __main__ would do that).
"""

import sys

from tdl.__main__ import main


if __name__ == "__main__":
    argv = list(sys.argv[1:])
    if not any(arg in ("gui",) for arg in argv) and not any(arg.startswith(("http://", "https://")) for arg in argv) and "-h" not in argv and "--help" not in argv:
        # No URL and no explicit subcommand: keep the user in the terminal.
        argv.append("tui")
    raise SystemExit(main(argv))
