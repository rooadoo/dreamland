# The base image and dependency locks target CPython 3.12 on Linux/amd64.
# Resolve and review a new digest deliberately when updating the release.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY deploy/requirements-build.txt /build/requirements-build.txt
RUN python -m pip install --require-hashes --only-binary=:all: -r requirements-build.txt
COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

LABEL org.opencontainers.image.title="DREAMLAND backend"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/requirements-runtime.txt /opt/dreamland/requirements-runtime.txt
COPY --from=wheel-builder /wheels /opt/dreamland/wheels
RUN python -m pip install --require-hashes --only-binary=:all: -r /opt/dreamland/requirements-runtime.txt \
    && python -m pip install --no-index --no-deps /opt/dreamland/wheels/*.whl \
    && python -m pip check

# Docker copies this directory's ownership into a fresh named volume. Neither
# initialization nor the SSH process needs to run as root.
RUN groupadd --gid 10001 dreamland \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/dreamland --no-create-home --shell /usr/sbin/nologin dreamland \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/dreamland /var/lib/dreamland/api /run/dreamland

USER 10001:10001
WORKDIR /var/lib/dreamland
EXPOSE 2222
# The existing daemon unwinds cleanup on Python's KeyboardInterrupt handler.
# Keep Docker's stop signal aligned until the daemon handles SIGTERM explicitly.
STOPSIGNAL SIGINT
CMD ["python", "-m", "dreamland.deployment", "serve-ssh", "--root", "/var/lib/dreamland"]
