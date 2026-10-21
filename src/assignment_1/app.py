"""FastAPI application: service instance, response models and routes."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

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


class GenerateRequest(BaseModel):
    """Prompt sent to the model."""

    model_config = ConfigDict(str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=4000)  # about 1k tokens


class GenerateResponse(BaseModel):
    """Text the model returned for a prompt."""

    completion: str


@app.post("/generate", response_model=GenerateResponse)
async def generate(
    body: GenerateRequest,
    model: Annotated[ModelClient, Depends(get_model_client)],
) -> GenerateResponse:
    """Return the model's completion for a prompt.

    The client call is synchronous, so it runs in a worker thread and the event
    loop keeps answering other requests, such as ``/health``, while the model works.
    Model failures become 504, 503 or 502 through ``model_error_handler``.

    Returns:
        GenerateResponse: the completion text.
    """
    completion = await asyncio.to_thread(model.generate, body.prompt)
    return GenerateResponse(completion=completion)
