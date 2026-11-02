# One image for every NTA process; docker-compose.yml picks the command and, per service,
# exactly which secrets the process gets.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ARG NTA_CODE_SHA=unknown
ENV NTA_CODE_SHA=${NTA_CODE_SHA} PATH=/app/.venv/bin:$PATH NTA_HEALTH_HOST=0.0.0.0
RUN useradd --create-home nta
USER nta
ENTRYPOINT ["nta"]
