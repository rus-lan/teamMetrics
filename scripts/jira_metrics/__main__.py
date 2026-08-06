"""Enables `python3 -m jira_metrics <command> ...` (run with `scripts/` on
sys.path, e.g. from inside that directory) as an alias for the
`scripts/jira-metrics` executable dispatcher — both call `cli.main()`."""

import sys

from . import cli

if __name__ == "__main__":
    raise SystemExit(cli.main(sys.argv[1:], invocation="python3 -m jira_metrics"))
