"""Frozen entry point for chatlog-keeper-gui.exe (PyInstaller, windowed).

Started without arguments it opens the desktop window.  The window runs every
action by starting this same executable again with ordinary chatlog-keeper
command-line arguments, so with arguments this file behaves exactly like
``chatlog_keeper_main.py``.
"""
import sys

if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0] == "--gui-selftest":
        from chatlog_keeper.gui import main as gui_main

        sys.exit(gui_main(arguments))

    if arguments[:1] == ["--_qq-sqlite-helper"]:
        # Same private frozen-child entry point as the command-line executable.
        from chatlog_keeper._qq_sqlite_helper import main as helper_main

        sys.exit(helper_main(arguments[1:]))

    from chatlog_keeper.cli import main

    sys.exit(main())
