# Arranger: API, web app and background worker in one image.
#
#   docker build -t arranger .
#   docker build -t arranger --build-arg WITH_AUDIO=0 --build-arg WITH_LILYPOND=0 .   # smallest image
#
# The same image runs either process:
#   web     arranger-api                      (default; also runs JOB_WORKERS in-process workers)
#   worker  arranger-worker --workers 2       (set JOB_WORKERS=0 on the web service when you use this)

# --- stage 1: the web app, built by Vite into static files -------------------
FROM node:22-bookworm-slim AS webapp

WORKDIR /build
COPY frontend-react/package.json frontend-react/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend-react/ ./
RUN npm run build

# --- stage 2: the Python image that serves the API and the built app ----------
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    RELOAD=false \
    COOKIE_SECURE=true \
    FRONTEND_DIR=/app/frontend

# PDF engraving (LilyPond) and audio decoding (libsndfile) are optional system packages.
# Without LilyPond the API reports PDF export as unavailable; MIDI and MusicXML still work.
ARG WITH_LILYPOND=1
ARG WITH_AUDIO=1
RUN set -eux; \
    packages=""; \
    if [ "$WITH_LILYPOND" = "1" ]; then packages="$packages lilypond"; fi; \
    if [ "$WITH_AUDIO" = "1" ]; then packages="$packages libsndfile1"; fi; \
    if [ -n "$packages" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends $packages; \
      rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

# `requirements.lock` pins every transitive dependency of the api, model and audio extras.
# It is used as a constraints file, so the build installs exactly what was tested.
COPY requirements.lock ./
RUN set -eux; \
    pip install --no-cache-dir --constraint requirements.lock ".[api,model]"; \
    if [ "$WITH_AUDIO" = "1" ]; then \
      pip install --no-cache-dir --constraint requirements.lock numpy onnxruntime soundfile soxr; \
      # basic-pitch declares a TensorFlow dependency with no wheels on this Python. Only its
      # bundled ONNX model file is used, so it is installed without dependencies.
      pip install --no-cache-dir --no-deps --constraint requirements.lock basic-pitch; \
    fi

# The built web app, and the notation engine it loads on demand: fetched at
# build time and checked against pinned SHA-256 digests.
COPY --from=webapp /build/dist ./frontend
COPY fetch_vendor.py ./
RUN python fetch_vendor.py --target frontend/vendor

# Run as an unprivileged user. /data holds SQLite and local artifacts when those backends are
# chosen; mount a volume there. With Postgres and ARTIFACT_BACKEND=database or s3 it stays empty.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin arranger \
    && mkdir -p /data \
    && chown -R arranger:arranger /data
USER arranger
ENV ARRANGER_DB_PATH=/data/arranger.db \
    ARTIFACT_DIR=/data/artifacts

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4).read()"

CMD ["arranger-api"]
