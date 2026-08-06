.PHONY: install uninstall init check run report doctor version release test

# Convenience wrapper only — every target below just calls the real entry
# point, scripts/jira-metrics (or install.sh/scripts/release.sh for
# install/uninstall/release). Nothing in this project depends on `make`
# existing; run scripts/jira-metrics, ./install.sh, or scripts/release.sh
# directly if you don't have it.

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

doctor:
	scripts/jira-metrics doctor

version:
	@cat VERSION

release:
	scripts/release.sh $(VERSION)

test:
	python3 -m unittest discover -s tests
