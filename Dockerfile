FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

# Install dependencies before copying source
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the application into the virtual environment
COPY README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create a non-root user
RUN useradd --create-home --uid 10001 appuser

# Copy the installed application and dependencies
COPY --from=builder /app/.venv /app/.venv
COPY models/ ./models/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "myapp.healthcheck"]

CMD ["uvicorn", "myapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
