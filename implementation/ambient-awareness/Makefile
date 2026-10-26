PYTHON ?= python3

.PHONY: test build verify
test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -v

build:
	mkdir -p build
	swiftc -warnings-as-errors butler_ambient/native/screenshot.swift -o build/butler-screen
	swiftc -warnings-as-errors butler_ambient/native/launcher.swift -o build/ButlerAwareness
	swiftc -warnings-as-errors butler_ambient/native/panel.swift -o build/butler-panel
	swiftc -warnings-as-errors butler_ambient/native/audio.swift -o build/butler-audio
	swiftc -warnings-as-errors butler_ambient/native/system-audio.swift -o build/butler-system-audio
	swiftc -warnings-as-errors butler_ambient/native/camera.swift -o build/butler-camera
	swiftc -warnings-as-errors butler_ambient/native/owner-auth.swift -o build/butler-owner-auth
	$(PYTHON) build_app.py
	$(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir build/wheels .

verify:
	$(PYTHON) verify.py
