"""FastAPI application: service instance, response models and routes."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import joblib  # type: ignore[import-untyped]  # joblib does not ship type information.
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from myapp.config import Settings

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the saved model once per service process, before serving requests."""
    app.state.model = joblib.load(settings.model_path)
    try:
        yield
    finally:
        del app.state.model


app = FastAPI(
    title=settings.name, version="0.1.0", debug=settings.debug, lifespan=lifespan
)

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


@app.post("/predict", response_model=PredictResponse)
async def predict(body: PredictRequest, request: Request) -> PredictResponse:
    """Predict a digit using the model loaded at startup."""
    time.sleep(1)
    prediction = request.app.state.model.predict([body.pixels])
    return PredictResponse(digit=int(prediction[0]))
