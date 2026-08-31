# Chaos Fidelity Standard — one-command checks.
# A machine-checkable standard must machine-check itself.

.PHONY: all test validate coverage

all: test

test: validate coverage  ## lint the catalog, prove wrong specs are rejected, check coverage

validate:
	python3 catalog/validate.py
	python3 catalog/test_validate.py

coverage:
	cd catalog && python3 coverage.py && python3 test_coverage.py
