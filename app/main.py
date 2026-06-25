from __future__ import annotations

import hmac
import json
import logging
from typing import Annotated, Any, Optional, Union
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.job_runner import JobNotFoundError, JobRunner, WorkerProcessingError
from app.schemas import (
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    ProcessJobRequest,
    ProcessJobResponse,
)
from app.supabase_client import create_repository

logger = logging.getLogger("recipe_dost.worker")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        for field in ("media_job_id", "exception_type"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    worker_logger = logging.getLogger("recipe_dost.worker")
    worker_logger.handlers.clear()
    worker_logger.addHandler(handler)
    worker_logger.setLevel(logging.INFO)
    worker_logger.propagate = False


configure_logging()

app = FastAPI(
    title="Recipe Dost Media Worker",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


class UnauthorizedWorkerError(Exception):
    pass


def require_worker_secret(
    x_worker_secret: Annotated[
        Optional[str],
        Header(alias="X-Worker-Secret"),
    ] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.worker_api_secret.get_secret_value()
    if x_worker_secret is None or not hmac.compare_digest(x_worker_secret, expected):
        raise UnauthorizedWorkerError


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@app.exception_handler(UnauthorizedWorkerError)
def handle_unauthorized_worker(
    _request: Request,
    _exc: UnauthorizedWorkerError,
) -> JSONResponse:
    return error_response(
        "unauthorized",
        "Missing or invalid worker secret.",
        401,
    )


@app.exception_handler(RequestValidationError)
def handle_request_validation(
    _request: Request,
    _exc: RequestValidationError,
) -> JSONResponse:
    return error_response(
        "invalid_request",
        "media_job_id must be a valid UUID.",
        422,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post(
    "/process-job",
    response_model=ProcessJobResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
def process_job(
    request: ProcessJobRequest,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> Union[ProcessJobResponse, JSONResponse]:
    try:
        runner = JobRunner(create_repository(settings))
        data = runner.run(request.media_job_id)
        return ProcessJobResponse(data=data)
    except JobNotFoundError:
        return error_response("job_not_found", "Media job not found.", 404)
    except WorkerProcessingError:
        return error_response("worker_error", "Media worker failed.", 500)
    except Exception as exc:
        log_request_failure(request.media_job_id, exc)
        return error_response("worker_error", "Media worker failed.", 500)


def log_request_failure(media_job_id: UUID, exception: Exception) -> None:
    logger.error(
        "worker_request_failed",
        extra={
            "media_job_id": str(media_job_id),
            "exception_type": type(exception).__name__,
        },
    )
