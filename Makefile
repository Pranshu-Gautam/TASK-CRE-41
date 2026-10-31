# TASK-CRE-41 — convenience targets. Everything here is a thin wrapper around
# the documented CLI commands in README.md; nothing is hidden in the Makefile.

DB_URI ?= postgresql+psycopg://iris:iris_local_dev_only@localhost:55432/iris
LOADER_DB_URI ?= postgresql+psycopg://iris_cre41_loader:loader_local_dev_only@localhost:55432/iris
COMPOSE ?= docker compose

.PHONY: help db build smoke init run verify samples test test-unit test-all ddl clean

help:
	@grep -E "^[a-z0-9-]+:.*?#" $(MAKEFILE_LIST) | sed 's/:.*#/\t#/'

db:            # start PostgreSQL 16 + PostGIS 3.4
	$(COMPOSE) up -d db

build:         # build the pipeline image
	$(COMPOSE) build pipeline

smoke:         # offline end-to-end smoke test inside Docker (no network, no DB)
	$(COMPOSE) run --rm pipeline smoke-test --skip-load

init:          # create the staging schema (iris_staging only)
	DB_URI=$(DB_URI) .venv/bin/iris-cre41 init-db

run:           # full live pipeline (~45 min, rate-limited)
	DB_URI=$(DB_URI) .venv/bin/iris-cre41 run-all

verify:        # read-only database evidence queries
	DB_URI=$(DB_URI) .venv/bin/iris-cre41 verify

samples:       # write output samples
	DB_URI=$(DB_URI) .venv/bin/iris-cre41 samples

ddl:           # regenerate sql/001_init_staging.sql from the SQLAlchemy metadata
	.venv/bin/iris-cre41 emit-ddl --out sql/001_init_staging.sql

test-unit:     # unit tests only (no network, no database)
	.venv/bin/python -m pytest -q -m "not integration"

test-all:      # unit + integration tests (requires a reachable DB_URI)
	DB_URI=$(DB_URI) LOADER_DB_URI=$(LOADER_DB_URI) .venv/bin/python -m pytest -q

verify-py311:  # prove the lock and the test suite work on Python 3.11
	docker build -f Dockerfile.py311 -t iris-cre41-py311 .
	docker run --rm iris-cre41-py311 python -c "import sys,shapely,pyproj; \
	  print(sys.version.split()[0], 'shapely', shapely.__version__, 'pyproj', pyproj.__version__)"
	docker run --rm iris-cre41-py311 python -m pytest -q -m "not integration" -p no:cacheprovider
	docker run --rm iris-cre41-py311 python -m iris_cre41.cli smoke-test --skip-load

verify-areas:  # prove stored areas are real ST_Area metrics of the source geometry
	DB_URI=$(DB_URI) AREA_EVIDENCE_OUT=samples/area_provenance_evidence.txt \
	  .venv/bin/python tools/validate_area_provenance.py

verify-role:   # database-level staging isolation evidence (needs LOADER_DB_URI)
	LOADER_DB_URI=$(LOADER_DB_URI) ADMIN_DB_URI=$(DB_URI) \
	ISOLATION_EVIDENCE_OUT=samples/role_isolation_evidence.txt \
	  .venv/bin/python tools/verify_role_isolation.py

test: test-unit

clean:         # remove pipeline artefacts and caches (keeps fixtures)
	rm -rf data/*.json data/*.sqlite .pytest_cache
