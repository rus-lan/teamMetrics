"""Central logging config for the `team_metrics` package.

Every module in this package logs through a logger named
`"team_metrics.<module>"` (see `get_logger`), all children of one
`"team_metrics"` logger. That parent logger carries a `NullHandler` from the
moment this module is imported, so any log call is silent-safe even if
`setup_logging()` is never invoked (an embedding process, or a subcommand
that takes no `--verbose`/`--quiet` flags, must not see logging's own
"no handlers found" fallback output).

`setup_logging()` is what turns logging on: it attaches one
`logging.StreamHandler` to `"team_metrics"`, writing to `sys.stderr` only —
`report report.json` writes the rendered HTML to stdout and is meant to be
piped, so stdout must stay clean of anything but that output.

`propagate` is set to False on `"team_metrics"` so this package never feeds
its records into the real root logger — an embedding process's own logging
config is left alone.
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"

_LOGGER_NAME = "team_metrics"

_root = logging.getLogger(_LOGGER_NAME)
_root.addHandler(logging.NullHandler())
_root.propagate = False


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Turns on logging for the whole `team_metrics` package.

    `quiet` wins if both `verbose` and `quiet` are set. Safe to call more
    than once — a second call only updates the level, it never attaches a
    second handler.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.propagate = False

    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logger.setLevel(level)

    # logging.FileHandler subclasses StreamHandler, so a plain isinstance
    # check would count a pre-attached FileHandler (e.g. from an embedding
    # process) as "already has our stderr handler" and skip adding it --
    # excluding FileHandler keeps this checking for the handler THIS
    # function attaches, not any StreamHandler-shaped one.
    has_stream_handler = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers
    )
    if not has_stream_handler:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
