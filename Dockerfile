# syntax=docker/dockerfile:1
# API + chat UI image (docs/deployment-plan.md D3). Serves only: ingest runs on GitHub Actions,
# and the container pulls the published index (scripts/railway-start.sh).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY corpus/sources.yaml corpus/schemes.yaml ./corpus/
COPY scripts/railway-start.sh ./scripts/
RUN uv sync --frozen --no-dev

# Bake the embedding model in at its pinned revision: no download at runtime, and the same
# weights as the refresh workflow (keep both equal to config.py's defaults, deployment D2).
ARG MF_FAQ_EMBED_MODEL=BAAI/bge-small-en-v1.5
ARG MF_FAQ_EMBED_MODEL_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
RUN python -c "from sentence_transformers import SentenceTransformer as S; \
S('${MF_FAQ_EMBED_MODEL}', revision='${MF_FAQ_EMBED_MODEL_REVISION}')"
ENV MF_FAQ_EMBED_MODEL=${MF_FAQ_EMBED_MODEL} \
    MF_FAQ_EMBED_MODEL_REVISION=${MF_FAQ_EMBED_MODEL_REVISION} \
    HF_HUB_OFFLINE=1 \
    MF_FAQ_DATA_DIR=/data

CMD ["sh", "scripts/railway-start.sh"]
