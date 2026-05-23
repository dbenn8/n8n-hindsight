FROM ghcr.io/vectorize-io/hindsight-api:latest-slim

USER root

RUN pip install --no-cache-dir supervisor

# CPU-only torch for local embeddings (no CUDA)
RUN pip install --no-cache-dir \
    --target /app/api/.venv/lib/python3.11/site-packages/ \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple/ \
    'torch>=2.0.0' \
    'sentence-transformers>=3.0.0' \
    'transformers>=4.41.0' \
    'scikit-learn' \
    'scipy' \
    'safetensors'

# Node.js + npm (for Control Plane) + nginx (for routing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm nginx \
    && npm install -g @vectorize-io/hindsight-control-plane@0.6.2 \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY nginx.conf /etc/nginx/nginx.conf
COPY supervisord.conf /etc/supervisord.conf
COPY scripts/ /app/scripts/

RUN mkdir -p /var/log/nginx /var/lib/nginx /run && chown -R hindsight:hindsight /var/log/nginx /var/lib/nginx /run

USER hindsight

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
