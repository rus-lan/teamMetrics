.PHONY: install uninstall init check run report test

# Convenience wrapper only — every target below just calls the real entry
# point, scripts/jira-metrics (or install.sh for install/uninstall).
# Nothing in this project depends on `make` existing; run scripts/jira-metrics
# or ./install.sh directly if you don't have it.

install:
	./install.sh

uninstall:
	./install.sh --uninstall

init:
	scripts/jira-metrics init

check:
	scripts/jira-metrics check

run:
	scripts/jira-metrics run $(ARGS)

report:
	scripts/jira-metrics report $(ARGS)

test:
	python3 -m unittest discover -s tests
