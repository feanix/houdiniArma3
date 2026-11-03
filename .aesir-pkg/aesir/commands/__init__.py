"""CLI commands, one module each. A module defines register(sub) and adds its parsers with set_defaults(func=...);
cli.py discovers every module here (sorted by name), so adding a command touches only its own file.

Optional module attributes: PRE_DB (set of command names run as func(args) before any database connect, errors
reported as 'error: ...' exit 1) and RAW (set of command names run as func(args) whose return value is the exit code)."""
