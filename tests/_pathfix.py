"""Adds `scripts/` to sys.path so `import team_metrics` works.

`python3 -m unittest discover -s tests` (this project's required test
command) treats `tests/` as its own top_level_dir and imports every test
module as a bare top-level name — tests/__init__.py is never actually
executed in that mode, and relative imports (`from . import helpers`) don't
work either, since the modules have no parent package at that point. Every
test module therefore does a plain `import _pathfix` as its first import
(this file is on sys.path because discover puts `tests/` itself there).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
