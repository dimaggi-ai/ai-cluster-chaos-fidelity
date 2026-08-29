# Chaos Fidelity Standard — one-command checks.
# A machine-checkable standard must machine-check itself.

.PHONY: all test validate

all: test

test: validate           ## lint the catalog + prove wrong specs are rejected

validate:
	python3 catalog/validate.py
	python3 catalog/test_validate.py
