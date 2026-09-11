FROM python:3.11-slim

WORKDIR /app

# curl is used by the container HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY requirements.txt .

# The engine is staged and installed OUTSIDE the workdir on purpose. Its repo
# layout is tcg_engine/tcg_engine/, so copying the outer directory into /app
# would leave /app/tcg_engine present at runtime; Python then imports
# "tcg_engine" as a namespace package pointing at that directory, shadowing the
# installed distribution. Submodules still resolve, so it appears to work until
# something imports the package itself.
#
# ebay_client is staged the same way and for the same reason: its layout is
# ebay_client/ebay_client/, so it would shadow itself identically.
#
# Both MUST be installed explicitly. requirements.txt lists them as editable
# installs and the grep below strips every '-e' line, so a local package that
# is not named here is simply absent from the image. app/main.py imports
# ebay_client at module load, which turned that omission into a container that
# would not start at all and a reverse proxy answering 502 for the whole site.
COPY tcg_engine/ /src/tcg_engine/
COPY ebay_client/ /src/ebay_client/
RUN pip install --no-cache-dir /src/tcg_engine "/src/ebay_client[notifications]" \
 && grep -v '^-e ' requirements.txt > /tmp/requirements-web.txt \
 && pip install --no-cache-dir -r /tmp/requirements-web.txt \
 && rm -rf /src /tmp/requirements-web.txt \
 && python -c "import tcg_engine.db, ebay_client.client"

# Copy application files
COPY app/ ./app/

# The one-off operational scripts. In the image because they are run with
# `docker exec` against the live databases and the live eBay connection --
# they need this container's environment, not a developer's laptop, where
# DATABASE_URL points at a different file entirely.
#
# Safe to sit inside /app, unlike tcg_engine and ebay_client above: the
# directory is named "scripts" and shadows no installed package.
COPY scripts/ ./scripts/

# Environment defaults
EXPOSE 8080
VOLUME /data

ENV DATABASE_URL="/data/inventory.db"
ENV USER_DATABASE_URL="/data/users.db"
ENV SESSION_SECRET_FILE="/data/.session_secret"
ENV PORT=8080
ENV COOKIE_SECURE="true"

# GOOGLE_CLIENT_ID has no sensible default: the app authenticates exclusively
# through Google Sign-In and refuses to start without one. Supply it via
# docker-compose or Container Manager.

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/api/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
