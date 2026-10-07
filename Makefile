PYTHON ?= python3
export PYTHONPATH := src

.PHONY: check lint typecheck test coverage validate secrets boundary history \
	publication-check release-consistency release-check sdist-check

check: lint typecheck test coverage validate secrets boundary

lint:
	$(PYTHON) -m ruff check .

# Configured entirely in pyproject.toml, so the arguments here and in CI stay
# empty and the two cannot drift apart.
typecheck:
	$(PYTHON) -m mypy

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

# Runs the suite a second time under the tracer. `check` keeps both because a
# failure under tracing is not the same signal as a failure without it, and
# because the coverage run swallows the per-test output.
coverage:
	$(PYTHON) tools/coverage.py --json build/coverage.json

validate:
	$(PYTHON) -m noetrail.cli validate

secrets:
	$(PYTHON) tools/check_secrets.py

boundary:
	$(PYTHON) tools/check_git_boundary.py

history:
	$(PYTHON) tools/check_history.py

# This intentionally inspects every reachable commit and is therefore kept
# out of the fast contributor gate. Run it before changing repository
# visibility or cutting a public release.
publication-check: secrets boundary history release-consistency

release-consistency:
	$(PYTHON) tools/check_release_consistency.py

# The acceptance run must exercise the installed distribution, so the local
# source path is removed from the environment it inherits.
release-check: release-consistency sdist-check
	rm -rf build dist src/noetrail.egg-info tools/noetrail.egg-info
	$(PYTHON) -m pip wheel . --no-deps --wheel-dir dist
	env -u PYTHONPATH $(PYTHON) tools/release_acceptance.py \
		--wheel $$(find dist -name 'noetrail-*.whl' -print -quit)

# The source distribution is what a distribution packager rebuilds and tests
# from, and a file missing from it is invisible everywhere else: the suite once
# shipped without `tests/__init__.py` and could not be discovered at all. The
# backend is called directly rather than through `python -m build`, so this
# target needs no tool beyond the setuptools the build already requires. The
# inherited PYTHONPATH is dropped so the unpacked archive is tested against its
# own sources instead of the checkout's.
sdist-check:
	rm -rf build/sdist dist src/noetrail.egg-info
	$(PYTHON) -c "import setuptools.build_meta as b; print(b.build_sdist('dist'))"
	mkdir -p build/sdist
	tar xzf dist/noetrail-*.tar.gz -C build/sdist
	cd build/sdist/noetrail-*/ && \
		env -u PYTHONPATH $(PYTHON) -m unittest discover -s tests -t . -v
