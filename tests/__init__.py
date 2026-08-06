"""Path bootstrap for package-style test runners (pytest, `unittest discover
-s tests -t .`). The project's required command,
`python3 -m unittest discover -s tests`, treats `tests/` itself as
top_level_dir and never imports this file — see tests/_pathfix.py, which
every test module imports directly for that case.
"""

from . import _pathfix  # noqa: F401
