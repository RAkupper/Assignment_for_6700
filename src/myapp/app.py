"""FastAPI application: service instance, response models and routes."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import joblib  # type: ignore[import-untyped]  # joblib does not ship type information.
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from myapp.config import Settings
from myapp.model_client import (
    ModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    build_model_client,
)

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the digits model and build the model client once, before serving."""
    app.state.model = joblib.load(settings.model_path)
    client = build_model_client(settings)
    app.state.model_client = client
    try:
        yield
    finally:
        client.close()
        del app.state.model


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


class ReadyResponse(BaseModel):
    """Typed body returned when the model backend is reachable."""

    status: Literal["ready"]


@app.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ErrorResponse}, 504: {"model": ErrorResponse}},
)
def ready(model: Annotated[ModelClient, Depends(get_model_client)]) -> ReadyResponse:
    """Confirm the model backend answers, unlike ``/health`` which only checks the app.

    A plain ``def`` route runs in a worker thread, so the blocking probe does not
    stall the event loop. Failures become 503 or 504 through ``model_error_handler``.

    Returns:
        ReadyResponse: ``ready`` when the model server answered.
    """
    model.ping()
    return ReadyResponse(status="ready")


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


Pixel = Annotated[int, Field(strict=True, ge=0, le=16)]


class PredictRequest(BaseModel):
    """A flattened 8x8 grayscale image in row-major order."""

    pixels: list[Pixel] = Field(
        min_length=64,
        max_length=64,
        description="64 integer pixels (0–16), read row by row from an 8×8 image.",
    )


class PredictResponse(BaseModel):
    """The digit recognized by the classifier."""

    digit: Annotated[int, Field(ge=0, le=9)]


@app.post("/predict", response_model=PredictResponse)
async def predict(body: PredictRequest, request: Request) -> PredictResponse:
    """Run blocking prediction in a worker thread, keeping the event loop free."""

    def run_prediction() -> PredictResponse:
        # Simulate slow inference with the same delay as blocking-before.
        time.sleep(1)
        prediction = request.app.state.model.predict([body.pixels])
        return PredictResponse(digit=int(prediction[0]))

    return await asyncio.to_thread(run_prediction)
