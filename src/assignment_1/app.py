"""FastAPI application: service instance, response models and routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from assignment_1.config import Settings
from assignment_1.model_client import (
    ModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    build_model_client,
)

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


class ErrorResponse(BaseModel):
    """Typed body returned when the model backend fails."""

    detail: str


@app.exception_handler(ModelClientError)
async def model_error_handler(request: Request, exc: ModelClientError) -> JSONResponse:
    """Turn model backend failures into 504/503/502 instead of a bare 500.

    The message names the failure but never echoes credentials or the prompt.
    """
    if isinstance(exc, ModelTimeoutError):
        status_code = 504
    elif isinstance(exc, ModelUnavailableError):
        status_code = 503
    else:
        status_code = 502
    body = ErrorResponse(detail=str(exc))
    return JSONResponse(status_code=status_code, content=body.model_dump())


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
