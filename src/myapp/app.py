"""FastAPI application: service instance, response models and routes."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
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
from myapp.ratelimit import SlidingWindowLimiter
from myapp.vectordb.search import Hit

settings = Settings()
generation_settings = GenerationSettings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load models once; generation shares the team's retrieval service."""
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


app = FastAPI(
    title=settings.name, version="0.1.0", debug=settings.debug, lifespan=lifespan
)
web_dir = Path(__file__).with_name("web")
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

PRIVATE_PATHS = {"/ask", "/retrieve"}  # questions can describe a user's visa status


@app.middleware("http")
async def no_store_private(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Keep questions and answers out of browser and proxy caches, errors included."""
    response = await call_next(request)
    if request.url.path in PRIVATE_PATHS:
        response.headers["Cache-Control"] = "no-store"
    return response


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


RETRY_AFTER_S = 30


class ErrorResponse(BaseModel):
    """Typed body returned when the model backend fails."""

    detail: str


@app.exception_handler(ModelClientError)
async def model_error_handler(request: Request, exc: ModelClientError) -> JSONResponse:
    """Turn model backend failures into 504/503/502 instead of a bare 500.

    The message names the failure but never echoes credentials or the prompt.
    """
    headers = {}
    if isinstance(exc, ModelTimeoutError):
        status_code = 504
    elif isinstance(exc, ModelUnavailableError):
        status_code = 503
        headers["Retry-After"] = str(RETRY_AFTER_S)  # usually a restart or a full queue
    else:
        status_code = 502
    body = ErrorResponse(detail=str(exc))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(), headers=headers
    )


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


asks = SlidingWindowLimiter()
retrievals = SlidingWindowLimiter()


def _enforce(limiter: SlidingWindowLimiter, request: Request, per_minute: int) -> None:
    """Answer 429 with Retry-After once a client goes over its limit.

    Behind Caddy, uvicorn sets ``request.client`` from X-Forwarded-For, so each
    user gets a limit rather than everyone sharing Caddy's address.
    """
    client = request.client.host if request.client else "unknown"
    wait = limiter.check(client, per_minute)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests: at most {per_minute} a minute",
            headers={"Retry-After": str(wait)},
        )


def limit_asks(request: Request) -> None:
    _enforce(asks, request, settings.rate_limit_ask_per_minute)


def limit_retrievals(request: Request) -> None:
    _enforce(retrievals, request, settings.rate_limit_retrieve_per_minute)


@app.post(
    "/retrieve",
    response_model=list[Hit],
    dependencies=[Depends(limit_retrievals)],
    responses={429: {"model": ErrorResponse}},
)
def retrieve(
    body: QuestionRequest,
    generation: Annotated[GenerationService, Depends(get_generation_service)],
) -> list[Hit]:
    """Inspect retrieved passages before involving generation."""
    return generation.retrieve(body)


@app.post(
    "/ask",
    response_model=Answer,
    dependencies=[Depends(limit_asks)],
    responses={429: {"model": ErrorResponse}},
)
def ask(
    body: QuestionRequest,
    generation: Annotated[GenerationService, Depends(get_generation_service)],
    model: Annotated[ModelClient, Depends(get_model_client)],
) -> Answer:
    """Retrieve evidence and generate an answer in FastAPI's worker thread."""
    return generation.ask(body, model)


admin_key = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin(key: Annotated[str | None, Depends(admin_key)]) -> None:
    """Let a request through only with the key from APP_ADMIN_API_KEY.

    404 when no key is configured, so the routes look absent; 401 for a missing
    or wrong key. compare_digest takes the same time however much of it matches.
    """
    if settings.admin_api_key is None:
        raise HTTPException(status_code=404, detail="Not Found")
    expected = settings.admin_api_key.get_secret_value().encode()
    if key is None or not secrets.compare_digest(key.encode(), expected):
        raise HTTPException(
            status_code=401, detail="A valid X-Admin-Key header is required"
        )


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@admin.post("/generate", response_model=GenerateResponse)
async def generate(
    body: GenerateRequest,
    model: Annotated[ModelClient, Depends(get_model_client)],
) -> GenerateResponse:
    """Return the model's completion for a prompt.

    The client call is synchronous, so it runs in a worker thread and the event
    loop keeps answering other requests, such as ``/health``, while the model works.
    Model failures become 504, 503 or 502 through ``model_error_handler``. It sends
    any prompt straight to the model server, so it sits behind ``require_admin``.

    Returns:
        GenerateResponse: the completion text.
    """
    completion = await asyncio.to_thread(model.generate, body.prompt)
    return GenerateResponse(completion=completion)


app.include_router(admin)
