# Multi-stage Dockerfile for duitku (Python).
#
# Stage 1 (builder): install duitku + its runtime deps into /opt/venv
# using a Debian-based slim image so wheels with native deps (e.g.
# uvicorn's optional uvloop) build cleanly when no wheel is published.
#
# Stage 2 (runtime): copy /opt/venv into a clean python:3.13-slim
# image. We do not use distroless-python yet because it ships without
# the small set of shared libs uvloop/httptools want, and the size win
# is modest.
#
# UID 65532 is intentional: it matches the nonroot user that the
# previous Go image (gcr.io/distroless/static:nonroot) used, so the
# Longhorn PVC at /landing — already owned by 65532 from files the
# Go pod wrote — remains accessible without an fsGroup change.

ARG PYTHON_VERSION=3.13.1

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# System build deps. Only needed if a transitive dep ships no manylinux
# wheel for cp313; kept minimal.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Create the venv we'll copy to runtime.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Install deps before source so layer caching survives source edits.
COPY pyproject.toml ./
COPY src/ ./src/
COPY README.md LICENSE ./

# Install duitku itself (which installs its declared runtime deps).
# Parsers are NOT installed in the webhook image — the eventual
# sweep/parse worker will install duitku[parsers] in its own image.
RUN pip install --no-cache-dir .


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# Create the nonroot user matching the existing PVC ownership.
RUN groupadd --system --gid 65532 nonroot \
 && useradd  --system --uid 65532 --gid 65532 --home-dir /home/nonroot \
            --create-home --shell /sbin/nologin nonroot

COPY --from=builder /opt/venv /opt/venv

USER 65532:65532
WORKDIR /home/nonroot
EXPOSE 8080
ENTRYPOINT ["duitku"]
CMD ["serve"]
