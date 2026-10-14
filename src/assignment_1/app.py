"""FastAPI application: service instance, response models and routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel

from assignment_1.config import Settings
from assignment_1.model_client import ModelClient, build_model_client

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the model client once at startup and close it at shutdown."""
    client = build_model_client(settings)
    app.state.model_client = client
    try:
        yield
    finally:
        client.close()


app = FastAPI(
    title=settings.name, version="0.1.0", debug=settings.debug, lifespan=lifespan
)


def get_model_client(request: Request) -> ModelClient:
    """Return the model client created at startup.

    Routes take it with ``Annotated[ModelClient, Depends(get_model_client)]`` so
    tests can swap in a fake through ``app.dependency_overrides``.
    """
    client: ModelClient = request.app.state.model_client
    return client


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
