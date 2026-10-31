# TASK-CRE-41 — reproducible execution image.
#
# The task requires Python 3.11+. This image uses 3.13 because the pinned
# lock in requirements.txt contains pyproj 3.8.0, which declares
# Requires-Python >= 3.12. The package itself supports 3.11 (see
# pyproject.toml); pin pyproj==3.7.2 instead if a 3.11 runtime is mandatory.
#
# Dependencies come from the pinned requirements.txt so the image is
# reproducible for a given lock.
FROM python:3.13-slim-bookworm

# Shapely/pyproj ship manylinux wheels with their own GEOS/PROJ, so no build
# toolchain or system GDAL is needed. Only certificates are required at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    IRIS_PROJECT_ROOT=/app

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY sql ./sql
COPY tests ./tests
COPY tools ./tools
RUN pip install --no-cache-dir --no-deps -e .

# Never run as root. compose.yaml overrides this with the host user id
# (APP_UID/APP_GID) so bind-mounted ./data and ./samples stay writable; the
# default below applies to a plain `docker run` without mounts.
RUN useradd --create-home --uid 10001 iris \
    && mkdir -p /app/data /app/samples /app/.pytest_cache \
    && chmod 0777 /app/data /app/samples /app/.pytest_cache \
    && chown -R iris:iris /app
USER iris

ENTRYPOINT ["iris-cre41"]
CMD ["--help"]
