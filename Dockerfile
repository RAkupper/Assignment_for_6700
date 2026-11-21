FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /usr/local/bin/uv

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


FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Create a non-root user and the folders it writes; new volumes copy this owner
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /embed-cache \
    && chown appuser:appuser /data /embed-cache

# Copy the installed application and dependencies
COPY --from=builder /app/.venv /app/.venv

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "myapp.healthcheck"]

CMD ["uvicorn", "myapp.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
