FROM ghcr.io/vectorize-io/hindsight-api:latest-slim

USER root

RUN pip install --no-cache-dir supervisor

COPY ops-proxy/package.json /opt/ops-proxy/package.json

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
    nodejs npm nginx gettext-base \
    && npm install --prefix /opt/ops-proxy --omit=dev \
    && npm install -g @vectorize-io/hindsight-control-plane@0.6.2 \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY nginx.conf /etc/nginx/nginx.conf.template
COPY start-nginx.sh /usr/local/bin/start-nginx.sh
COPY supervisord.conf /etc/supervisord.conf
COPY scripts/ /app/scripts/

RUN chmod +x /usr/local/bin/start-nginx.sh \
    && mkdir -p /var/log/nginx /var/lib/nginx /run \
    && chown -R hindsight:hindsight /var/log/nginx /var/lib/nginx /run /etc/nginx

# Durable volume logging: writer + supervisord wrapper, log dir on /data.
COPY logwriter/logwriter.py /opt/logwriter.py
COPY logpipe.sh /usr/local/bin/logpipe.sh
RUN chmod +x /usr/local/bin/logpipe.sh && mkdir -p /data/logs && chown -R hindsight:hindsight /data/logs

# Ops-proxy admin service (standalone FastAPI; serves GET /logs).
COPY ops-proxy/ /opt/ops-proxy/
RUN pip install --no-cache-dir -r /opt/ops-proxy/requirements.txt

USER hindsight

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
