# WildStak server side: renders the dashboard cards and the manifest the
# device polls. Run from the repo root. Layout:
#
#   dashboards/*_dashboard.py  - one script per dashboard card
#   dashboards/dashboardlib.py - shared library, not a card
#   dashboards/*.py            - other helper modules a card may import
#                                (e.g. a timetable parser), not cards
#   dashboards/config.toml     - global + per-card config (copy
#                                config.example.toml; see DASHBOARD_SPEC.md)
#   dashboards/fonts/          - fonts referenced by config.toml
#   .env                       - Home Assistant long-lived access token, plain
#                                text (just the token), NOT KEY=VALUE syntax
#   local.mk                   - your settings, at least BASE_URL (gitignored)
#   docs/media/                - rendered card PNGs (created if missing)
#   docs/data/                 - generated URL manifest (created if missing)
#
# Two ways to publish:
#
#   make       - cards only: PNGs + manifest under docs/. Serve docs/ with
#                any static web server at BASE_URL.
#   make site  - the same, plus docs/dashboards.md (an index page) and a
#                Zensical build of the site around it.
#
# Every dashboards/*_dashboard.py script is picked up automatically and is
# expected to follow the CLI contract in DASHBOARD_SPEC.md - in short:
#
#   <script>.py --config CONFIG.toml --key-file KEY_FILE -o OUTPUT.png
#
# producing exactly one PNG, with a module docstring describing it, and a
# [cards.<card_name>] table in config.toml. To add a new dashboard: drop a
# *_dashboard.py script into dashboards/, add its config table, run `make`,
# done - no edits to this file needed. Anything else in dashboards/ (shared
# library code, helper modules a card imports) just needs to NOT match
# *_dashboard.py. docs/dashboards.md gets one "## <script name>" section
# per script, each with that script's docstring and its PNG.
#
# One cron job (`make -C /path/to/wildstak`, or `make -C ... site`) keeps
# everything current: it re-renders every card unconditionally (they're
# data-driven, not source-driven, so plain file-mtime staleness checks
# would serve stale energy data) and regenerates the manifest.

# --- Configuration: set in local.mk or on the command line --------------
-include local.mk
# Public URL the device fetches cards from: where docs/ is served, so
# $(BASE_URL)/data/dashboards.txt is the manifest URL in firmware/device.yaml.
BASE_URL       ?=
DASHBOARDS_DIR ?= dashboards
MEDIA_DIR      ?= docs/media
DATA_DIR       ?= docs/data
MANIFEST       ?= $(DATA_DIR)/dashboards.txt
DASHBOARDS_PAGE ?= docs/dashboards.md
KEY_FILE       ?= .env
CONFIG         ?= $(DASHBOARDS_DIR)/config.toml
# The venv from requirements.txt if there is one, else whatever python3 is.
PYTHON         ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
# -------------------------------------------------------------------------

# Only *_dashboard.py counts as a card - dashboardlib.py and any standalone
# helper module a card imports (e.g. a timetable parser) don't match
# this pattern, so they're simply never swept in (no exclusion list needed).
DASHBOARD_SCRIPTS := $(wildcard $(DASHBOARDS_DIR)/*_dashboard.py)
DASHBOARD_IMAGES  := $(patsubst $(DASHBOARDS_DIR)/%.py,$(MEDIA_DIR)/%.png,$(DASHBOARD_SCRIPTS))
# Path to the manifest relative to docs/, for linking from dashboards.md.
MANIFEST_REL      := $(patsubst docs/%,%,$(MANIFEST))

.DEFAULT_GOAL := cards
.PHONY: cards site dashboards manifest dashboards_page check_base_url clean help

cards: check_base_url dashboards manifest

# Cards, manifest, and the index page are regenerated before the site build
# so Zensical picks up current content, not whatever was there from the
# last run.
site: cards dashboards_page
	zensical build

# Listed first wherever it's needed, so a missing BASE_URL fails before
# minutes of rendering rather than after.
check_base_url:
	@test -n "$(BASE_URL)" || { echo "BASE_URL is not set: add BASE_URL = https://... to local.mk"; exit 1; }

# Deliberately .PHONY rather than a pattern rule keyed on the script's
# mtime: these render live Home Assistant data, so every invocation must
# re-run every script regardless of whether its source changed.
dashboards: | $(MEDIA_DIR)
	@for script in $(DASHBOARD_SCRIPTS); do \
		echo "==> $$script"; \
		$(PYTHON) "$$script" --config $(CONFIG) --key-file $(KEY_FILE) \
			-o "$(MEDIA_DIR)/$$(basename "$$script" .py).png" || exit 1; \
	done

# Manifest format (see NOTES.md, "Firmware redesign (v0.3)"):
#   line 1  - ISO8601 UTC timestamp of this `make` run ("cards generated at")
#   line 2+ - one card per line: "<hash> <url>", where <hash> is the first 16
#             hex chars of the PNG's SHA-256. The device downloads a card only
#             when its hash differs from the one stored with its flash-cached
#             copy. A content hash, because every `make` run rewrites every
#             PNG (new mtime, new ETag) whether or not the pixels changed.
# Line order is the rotation order: card i is the device's cache slot i.
# The file is written to a temp name and renamed, so a device fetching
# mid-run never sees a half-written manifest.
manifest: check_base_url dashboards | $(DATA_DIR)
	@{ \
		date -u +%Y-%m-%dT%H:%M:%SZ; \
		for img in $(DASHBOARD_IMAGES); do \
			echo "$$(sha256sum "$$img" | cut -c1-16) $(BASE_URL)/media/$$(basename "$$img")"; \
		done; \
	} > $(MANIFEST).tmp
	@mv $(MANIFEST).tmp $(MANIFEST)
	@echo "Wrote $(MANIFEST)"

# Docstrings are read with `ast` (no import, no execution) so this never
# triggers argparse, network calls, or requires --key-file/--config itself.
dashboards_page: dashboards manifest
	@: > $(DASHBOARDS_PAGE)
	@{ \
		echo "[Dashboard URL manifest]($(MANIFEST_REL))"; \
		echo; \
	} >> $(DASHBOARDS_PAGE)
	@for script in $(DASHBOARD_SCRIPTS); do \
		name=$$(basename "$$script" .py); \
		{ \
			echo "## $$name"; \
			echo; \
			$(PYTHON) -c "import ast, sys; print((ast.get_docstring(ast.parse(open(sys.argv[1]).read())) or '').strip())" "$$script"; \
			echo; \
			echo "![$$name](media/$$name.png)"; \
			echo; \
		} >> $(DASHBOARDS_PAGE); \
	done
	@echo "Wrote $(DASHBOARDS_PAGE)"

$(MEDIA_DIR) $(DATA_DIR):
	mkdir -p $@

clean:
	rm -f $(DASHBOARD_IMAGES) $(MANIFEST) $(DASHBOARDS_PAGE)

help:
	@echo "make [cards]          - render every card and write the manifest (serve docs/)"
	@echo "make dashboards       - render card PNGs only"
	@echo "make manifest         - render cards, then write the manifest"
	@echo "make site             - cards + docs/dashboards.md + zensical build"
	@echo "make dashboards_page  - render cards, then write docs/dashboards.md"
	@echo "make clean            - remove generated PNGs, manifest, and index page"
