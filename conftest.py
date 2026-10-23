"""Collection rules for the whole repository."""
import sys

# tests_arrkit needs arrkit (Python 3.12 or newer, not on PyPI) and tomllib (3.11+). On an older interpreter a bare `pytest .` would fail
# to collect it, so the directory is left out there; CI runs `test/` only, and `pytest tests_arrkit` runs it wherever arrkit is installed.
collect_ignore = ["tests_arrkit"] if sys.version_info < (3, 12) else []
