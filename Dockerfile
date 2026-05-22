FROM ghcr.io/vectorize-io/hindsight-api:latest-slim

USER root

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

USER hindsight

CMD ["python", "-m", "hindsight_api"]
