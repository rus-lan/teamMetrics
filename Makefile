.PHONY: init check run report test

# Convenience wrapper only — every target below just calls the real entry
# point, scripts/jira-metrics. Nothing in this project depends on `make`
# existing; run scripts/jira-metrics directly if you don't have it.

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
