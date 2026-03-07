FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first (cache-friendly layer)
COPY pyproject.toml ./

# Install dependencies (no dev deps, no workspace — standalone install)
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy application code
COPY app/ ./app/

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
