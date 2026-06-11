# Dedicated workflow-validator image.
#
# BUILD CONTEXT QUIRK: this image MUST be built from the REPO ROOT with
#   dockerfile_path=validator-app/Dockerfile
# because the COPY instructions below reference paths under ops-proxy/
# (e.g. `COPY ops-proxy/package.json ...`). Building from inside
# validator-app/ as the context root will fail — those paths won't exist.
#
# better-sqlite3 (a native dependency pulled in by n8n-mcp) is compiled
# from source during `npm install`, so the C/C++ build toolchain
# (python3, make, g++) must be present BEFORE the install. We install it
# up front and clean the apt lists in the same layer for a deterministic,
# self-contained build.
FROM node:20-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Build toolchain for native modules (better-sqlite3 via n8n-mcp) plus
# python3 for the FastAPI service. make + g++ are required by node-gyp to
# compile better-sqlite3 deterministically from source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ops-proxy/package.json /app/package.json
RUN npm install --omit=dev

COPY ops-proxy/ /app/
RUN pip3 install --break-system-packages --no-cache-dir -r /app/requirements.txt

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
