# Cloud Build uses this when deploying from GitHub, which is more predictable
# than the Python buildpack for a uv-locked project.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so edits to the source do not invalidate the layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Cloud Run injects PORT; app.py reads it and defaults to 8000 locally.
EXPOSE 8080
CMD ["python", "app.py"]
