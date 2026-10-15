"""FastAPI application: service instance, response models and routes."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from myapp.config import Settings

settings = Settings()
app = FastAPI(title=settings.name, version="0.1.0", debug=settings.debug)


class HealthResponse(BaseModel):
    """Typed body returned by the health check."""

    status: Literal["ok"]
    version: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Confirm the service is running.

    Returns:
        HealthResponse: an ok status and the running service version.
    """
    return HealthResponse(status="ok", version=app.version)
