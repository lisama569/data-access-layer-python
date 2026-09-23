"""FastAPI 应用装配：初始化本地数据库、注册路由与统一错误处理。

错误归类（HTTP 状态码 + 可区分 reason）：
- VersionConflictError -> 409 reason=version_conflict（携带期望/实际版本号）
- ConflictError        -> 409 reason=<具体原因>
- NotFoundError        -> 404 reason=not_found
- InvalidCursorError   -> 400 reason=invalid_cursor
- StorageError         -> 500 reason=storage_error（不含驱动原始细节）
- 其余未知异常          -> 500 reason=internal_error
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router as resource_router
from app.database import create_connection, get_db_path, init_schema
from app.errors import (
    ConflictError,
    InvalidCursorError,
    NotFoundError,
    StorageError,
    VersionConflictError,
)

logger = logging.getLogger("dal")


def _error_response(
    status_code: int, error: str, reason: str, detail: str = ""
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "reason": reason, "detail": detail},
    )


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 幂等建表，保证本地数据库就绪。
        conn = create_connection(get_db_path())
        try:
            init_schema(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(title="Data Access Layer Service", lifespan=lifespan)

    @app.exception_handler(VersionConflictError)
    def _version_conflict(request: Request, exc: VersionConflictError) -> JSONResponse:
        logger.warning(
            "version conflict id=%s expected=%s actual=%s",
            exc.resource_id,
            exc.expected_version,
            exc.actual_version,
        )
        return _error_response(
            409,
            error="conflict",
            reason="version_conflict",
            detail=(
                f"resource {exc.resource_id}: expected version "
                f"{exc.expected_version}, actual {exc.actual_version}"
            ),
        )

    @app.exception_handler(ConflictError)
    def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(409, error="conflict", reason=exc.reason, detail=str(exc))

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(404, error="not_found", reason="not_found", detail=str(exc))

    @app.exception_handler(InvalidCursorError)
    def _invalid_cursor(request: Request, exc: InvalidCursorError) -> JSONResponse:
        return _error_response(400, error="bad_request", reason="invalid_cursor", detail=str(exc))

    @app.exception_handler(StorageError)
    def _storage(request: Request, exc: StorageError) -> JSONResponse:
        # 只记录固定文案，绝不把底层驱动异常透出给调用方。
        logger.error("storage error: %s", exc)
        return _error_response(500, error="internal", reason="storage_error")

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return _error_response(500, error="internal", reason="internal_error")

    app.include_router(resource_router)
    return app


app = create_app()
