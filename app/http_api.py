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
    ResourceNotFoundError,
    UniqueConstraintError,
    VersionConflictError,
)
from app.connection import Database
from app.models import ResourceCreate, ResourceQuery, ResourceUpdate
from app.service import ResourceService

_STATUS_MAP: dict[str, int] = {
    InvalidInputError.code: 400,
    InvalidCursorError.code: 400,
    ResourceNotFoundError.code: 404,
    VersionConflictError.code: 409,
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


def _resource_to_dict(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "payload": r.payload,
        "version": r.version,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def create_app(db: Database) -> FastAPI:
    app = FastAPI(title="persistent-dal")
    service = ResourceService(db)
    app.state.service = service

    @app.exception_handler(DataAccessError)
    async def _handle_dal_error(_: Request, exc: DataAccessError) -> JSONResponse:
        status = _STATUS_MAP.get(exc.code, 500)
        body: dict = {"error": {"code": exc.code, "message": exc.message}}
        if isinstance(exc, VersionConflictError):
            body["error"]["resource_id"] = exc.resource_id
            body["error"]["expected_version"] = exc.expected_version
            body["error"]["current_version"] = exc.current_version
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

    @app.get("/resources")
    def list(
        name_contains: str | None = None,
        page_size: int = 20,
        cursor: str | None = None,
    ):
        page = service.list_resources(
            ResourceQuery(
                name_contains=name_contains,
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
