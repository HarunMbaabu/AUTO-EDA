# Brains Bot image, used by Render (render.yaml) and any Docker host.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.11.32

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project

COPY README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --locked

RUN useradd --create-home --uid 10001 bot && mkdir -p data/processed && chown -R bot data
USER bot

# Secrets come from the host's environment variables; no .env is baked into the image.
CMD ["brains-bot"]
