# JARVIS headless core - container image for Oracle Cloud (or any other
# Docker-capable host). Builds the same core.headless_main entry point
# Render already runs successfully from this same requirements.txt, so
# this doesn't introduce a second, divergent dependency set.
#
# Does NOT run the desktop app (main.py) - that needs PyQt6, a sound
# device, and a display, none of which exist in a container. This image
# is the cloud/headless half only, matching the existing Render split.
FROM python:3.12-slim

# Playwright's Chromium needs these system libraries present even in
# headless mode. Installed once here rather than relying on
# `playwright install --with-deps`, which needs root + apt at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install chromium

COPY . .

# Data/log persistence - mount a real volume here in production
# (`docker run -v jarvis_data:/app/data`). Without a mounted volume this
# is still writable, it just won't survive a container replacement.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV JARVIS_HEADLESS_HOST=0.0.0.0
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8787}/health || exit 1

CMD ["python", "-m", "core.headless_main"]
