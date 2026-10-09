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

# 2026-10-09: the unpinned base image (latest-slim) moved its huggingface_hub
# and the unpinned `transformers` installed above then failed its import at API
# startup (ImportError: cannot import name 'ResolvedRevision'), so deploy 233644
# shipped a crash-looping API (502) behind a green Appliku build. Same failure
# class hit the personal instance (hindsight-deploy) the same morning; this is
# the same remedy: purge the Hugging Face set the base and the --target layer
# left interleaved, reinstall it PINNED to the versions the 2026-09-26 image
# ran with (hf_xet stays at 1.6.0 because 1.7.0 drops XetFileInfo, which
# huggingface_hub 1.33.0 imports), then GATE THE BUILD on the imports the API
# needs at startup. A venv that cannot import them must fail the build here,
# never ship and 502 in prod.
RUN cd /app/api/.venv/lib/python3.11/site-packages/ \
    && rm -rf typing_extensions-4.15*.dist-info \
        tokenizers tokenizers-*.dist-info \
        transformers transformers-*.dist-info \
        sentence_transformers sentence_transformers-*.dist-info \
        huggingface_hub huggingface_hub-*.dist-info \
        hf_xet hf_xet-*.dist-info hf_xet.libs \
    && pip install --no-cache-dir --force-reinstall --no-deps --target . \
        typing_extensions==4.16.0 tokenizers==0.23.2 transformers==5.17.0 \
        sentence-transformers==6.1.0 huggingface_hub==1.33.0 hf_xet==1.6.0 \
    && /app/api/.venv/bin/python -c "import hindsight_api.main; import sentence_transformers; import huggingface_hub.file_download; from hf_xet import XetFileInfo; print('venv import gate: OK')"

# Node.js + npm (for Control Plane) + nginx (for routing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm nginx gettext-base \
    && npm install -g @vectorize-io/hindsight-control-plane@0.6.2 \
    && apt-get purge -y npm && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY nginx.conf /etc/nginx/nginx.conf.template
COPY start-nginx.sh /usr/local/bin/start-nginx.sh
COPY supervisord.conf /etc/supervisord.conf
COPY scripts/ /app/scripts/
COPY start.sh /usr/local/bin/start.sh

RUN chmod +x /usr/local/bin/start-nginx.sh /usr/local/bin/start.sh \
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

CMD ["/usr/local/bin/start.sh"]
