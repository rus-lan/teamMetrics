.PHONY: install uninstall init check run report doctor version release test

# Convenience wrapper only — every target below just calls the real entry
# point, scripts/team-metrics (or install.sh/scripts/release.sh for
# install/uninstall/release). Nothing in this project depends on `make`
# existing; run scripts/team-metrics, ./install.sh, or scripts/release.sh
# directly if you don't have it.

install:
	./install.sh

uninstall:
	./install.sh --uninstall

init:
	scripts/team-metrics init

check:
	scripts/team-metrics check

run:
	scripts/team-metrics run $(ARGS)

report:
	scripts/team-metrics report $(ARGS)

doctor:
	scripts/team-metrics doctor

version:
	@cat VERSION

release:
	scripts/release.sh $(VERSION)

test:
	python3 -m unittest discover -s tests
