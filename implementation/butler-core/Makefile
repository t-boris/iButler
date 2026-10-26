PYTHON ?= python3

.PHONY: test build verify
test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -v

build:
	mkdir -p build
	swiftc -warnings-as-errors native/launcher.swift -o build/ButlerCore
	$(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir build/wheels .

verify:
	$(PYTHON) verify.py
