# Use a slim Python image for the application
FROM python:3.14-slim

# Install uv as the package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set timezone to local for log improvements
ENV TZ="Europe/Stockholm"

# Ensures Python's stdout/stderr goes directly to the container logs unbuffered
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer caches independently of the source
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Application source
COPY src/ ./src
ENV PYTHONPATH=/app/src

EXPOSE 8000

# start.py resolves .env relative to the working directory. There is no .env in
# the image by design — configuration comes from the k8s ConfigMap and Secret.
WORKDIR /app/src
CMD ["python", "start.py"]
