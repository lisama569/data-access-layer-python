"""FastAPI HTTP 入口。

所有 :class:`app.errors.DataAccessError` 在这里被统一转译为结构化 HTTP
响应，``code`` 字段用于区分冲突原因；底层存储异常不会以原始类型透出。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.errors import (
    DataAccessError,
    InvalidCursorError,
    InvalidInputError,
    InvalidStateError,
    ResourceNotFoundError,
    UniqueConstraintError,
    VersionConflictError,
)
from app.connection import Database
from app.models import (
    ResourceCreate,
    ResourceQuery,
    ResourceStateChange,
    ResourceUpdate,
)
from app.service import ResourceService

_STATUS_MAP: dict[str, int] = {
    InvalidInputError.code: 400,
    InvalidCursorError.code: 400,
    ResourceNotFoundError.code: 404,
    VersionConflictError.code: 409,
    InvalidStateError.code: 409,
    UniqueConstraintError.code: 409,
    DataAccessError.code: 500,
}


class CreateBody(BaseModel):
    name: str = Field(min_length=1)
    payload: str = ""
    id: str | None = None


class UpdateBody(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = None
    payload: str | None = None


class StateChangeBody(BaseModel):
    expected_version: int = Field(ge=1)


class PurgeBody(BaseModel):
    retention_ms: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=1)
    max_batches: int | None = Field(default=None, ge=1)


def _resource_to_dict(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "payload": r.payload,
        "version": r.version,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "status": r.status,
        "deleted_at": r.deleted_at,
    }


def create_app(db: Database, *, clock=None) -> FastAPI:
    app = FastAPI(title="persistent-dal")
    service = ResourceService(db, clock=clock) if clock is not None else ResourceService(db)
    app.state.service = service

    @app.exception_handler(DataAccessError)
    async def _handle_dal_error(_: Request, exc: DataAccessError) -> JSONResponse:
        status = _STATUS_MAP.get(exc.code, 500)
        body: dict = {"error": {"code": exc.code, "message": exc.message}}
        if isinstance(exc, VersionConflictError):
            body["error"]["resource_id"] = exc.resource_id
            body["error"]["expected_version"] = exc.expected_version
            body["error"]["current_version"] = exc.current_version
        elif isinstance(exc, InvalidStateError):
            body["error"]["resource_id"] = exc.resource_id
            body["error"]["current_status"] = exc.current_status
        return JSONResponse(status_code=status, content=body)

    @app.post("/resources", status_code=201)
    def create(body: CreateBody):
        resource = service.create_resource(
            ResourceCreate(name=body.name, payload=body.payload, id=body.id)
        )
        return _resource_to_dict(resource)

    @app.get("/resources/{resource_id}")
    def get_one(resource_id: str):
        return _resource_to_dict(service.get_resource(resource_id))

    @app.put("/resources/{resource_id}")
    def update(resource_id: str, body: UpdateBody):
        resource = service.update_resource(
            ResourceUpdate(
                id=resource_id,
                expected_version=body.expected_version,
                name=body.name,
                payload=body.payload,
            )
        )
        return _resource_to_dict(resource)

    @app.delete("/resources/{resource_id}")
    def soft_delete(resource_id: str, body: StateChangeBody):
        resource = service.delete_resource(
            ResourceStateChange(
                id=resource_id, expected_version=body.expected_version
            )
        )
        return _resource_to_dict(resource)

    @app.post("/resources/{resource_id}/restore")
    def restore(resource_id: str, body: StateChangeBody):
        resource = service.restore_resource(
            ResourceStateChange(
                id=resource_id, expected_version=body.expected_version
            )
        )
        return _resource_to_dict(resource)

    @app.post("/resources/purge")
    def purge(body: PurgeBody):
        result = service.purge_deleted(
            retention_ms=body.retention_ms,
            batch_size=body.batch_size,
            max_batches=body.max_batches,
        )
        return {
            "purged": result.purged,
            "scanned": result.scanned,
            "has_more": result.has_more,
            "batches": result.batches,
        }

    @app.get("/resources")
    def list(
        name_contains: str | None = None,
        status: str = "active",
        page_size: int = 20,
        cursor: str | None = None,
    ):
        page = service.list_resources(
            ResourceQuery(
                name_contains=name_contains,
                status=status,
                page_size=page_size,
                cursor=cursor,
            )
        )
        return {
            "items": [_resource_to_dict(i) for i in page.items],
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    return app
