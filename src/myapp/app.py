"""FastAPI application: service instance, response models and routes."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import joblib  # type: ignore[import-untyped]  # joblib does not ship type information.
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from myapp.config import Settings
from myapp.generation.schemas import Answer, QuestionRequest
from myapp.generation.service import GenerationService, GenerationSettings
from myapp.model_client import (
    EchoModelClient,
    ModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    build_model_client,
)
from myapp.vectordb.search import Hit

settings = Settings()
generation_settings = GenerationSettings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load models once; generation shares the team's retrieval service."""
    app.state.model = joblib.load(settings.model_path)
    client = build_model_client(settings)
    app.state.model_client = client
    generation: GenerationService | None = None
    try:
        if generation_settings.enabled:
            generation = await asyncio.to_thread(
                GenerationService.from_settings, generation_settings
            )
        app.state.generation = generation
        yield
    finally:
        app.state.generation = None
        if generation is not None:
            generation.close()
        client.close()
        del app.state.model


app = FastAPI(
    title=settings.name, version="0.1.0", debug=settings.debug, lifespan=lifespan
)
web_dir = Path(__file__).with_name("web")
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the same-origin prototype UI without a separate frontend server."""
    return FileResponse(web_dir / "index.html")


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
    """All dependencies for the enabled API features passed their probes."""

    status: Literal["ready"]


@app.get(
    "/ready",
    response_model=ReadyResponse,
    responses={
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def ready(
    request: Request,
    model: Annotated[ModelClient, Depends(get_model_client)],
) -> ReadyResponse:
    """Probe the configured LLM and, when enabled, loaded retrieval dependencies.

    Metadata checks run in a worker thread, without generating an answer or
    performing embeddings. Failures preserve the existing 502/503/504 mapping.
    """
    model.ping()
    if generation_settings.enabled:
        if isinstance(model, EchoModelClient):
            raise ModelUnavailableError("generation requires a real model backend")
        get_generation_service(request).check_ready()
    return ReadyResponse(status="ready")


class GenerateRequest(BaseModel):
    """Prompt sent to the model."""

    model_config = ConfigDict(str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=4000)  # about 1k tokens


class GenerateResponse(BaseModel):
    """Text the model returned for a prompt."""

    completion: str


def get_generation_service(request: Request) -> GenerationService:
    service: GenerationService | None = getattr(request.app.state, "generation", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Generation disabled; set GENERATION_ENABLED=true and restart",
        )
    return service


@app.post("/retrieve", response_model=list[Hit])
def retrieve(
    body: QuestionRequest,
    generation: Annotated[GenerationService, Depends(get_generation_service)],
) -> list[Hit]:
    """Inspect retrieved passages before involving generation."""
    return generation.retrieve(body)


@app.post("/ask", response_model=Answer)
def ask(
    body: QuestionRequest,
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    model: Annotated[ModelClient, Depends(get_model_client)],
) -> Answer:
    """Retrieve evidence and generate an answer in FastAPI's worker thread."""
    return generation.ask(body, model)


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
