# syntax=docker/dockerfile:1

# ── Base ─────────────────────────────────────────────────────────────────────
# Python 3.13 to match the development environment. "slim" keeps the image small;
# Chromium's OS libraries are added explicitly below by `playwright install`.
FROM python:3.13-slim

# Unbuffered, .pyc-free logs that stream cleanly into Render's log viewer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
# Copy requirements first so this layer is cached and only rebuilt when deps change.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r backend/requirements.txt

# ── Browser + system libraries for ScrapeGraphAI / Playwright ────────────────
# ScrapeGraphAI drives Playwright/Chromium under the hood, so the browser AND its
# system libraries (libnss3, libatk, fonts, …) must be present. The build runs as
# root, so `--with-deps` can apt-install those libraries — the clean path that
# Render's native (non-Docker) build environment cannot take.
RUN playwright install --with-deps chromium

# ── Application code ─────────────────────────────────────────────────────────
# (.dockerignore keeps .env, backend/jobs/, caches and data files out of the image.)
COPY backend ./backend
COPY frontend ./frontend

# `main:app` lives in backend/; run from there so the module import resolves and
# the app's __file__-relative paths (pipeline sys.path, ../frontend) work unchanged.
WORKDIR /app/backend

# Bind to all interfaces and read the port Render injects via $PORT — never
# hardcoded (falls back to 10000, Render's default, for a plain `docker run`).
# Single worker: the app holds job state in memory and guards on one job at a time,
# so more than one worker/instance would break that invariant.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1
