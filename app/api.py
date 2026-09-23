"""FastAPI 路由：把 HTTP 入参映射到服务层，错误由 app.main 统一归类。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.models import Page, Resource
from app.schemas import PageOut, ResourceCreate, ResourceOut, ResourceUpdate
from app.services import ResourceService

router = APIRouter(prefix="/resources", tags=["resources"])


def _service(request: Request) -> ResourceService:
    # 每个请求一个服务实例（读用独立连接，写用独立事务）。
    return ResourceService()


def _to_out(r: Resource) -> ResourceOut:
    return ResourceOut(
        id=r.id,
        name=r.name,
        status=r.status,
        version=r.version,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.post("", response_model=ResourceOut, status_code=201)
def create_resource(body: ResourceCreate, request: Request) -> ResourceOut:
    created = _service(request).create_resource(body.name, body.status)
    return _to_out(created)


@router.get("", response_model=PageOut)
def list_resources(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str = Query(""),
    status: str | None = Query(None),
    name_contains: str | None = Query(None),
) -> PageOut:
    page: Page[Resource] = _service(request).list_resources(
        limit=limit, cursor=cursor, status=status, name_contains=name_contains
    )
    return PageOut(
        items=[_to_out(r) for r in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get("/{resource_id}", response_model=ResourceOut)
def get_resource(resource_id: str, request: Request) -> ResourceOut:
    return _to_out(_service(request).get_resource(resource_id))


@router.put("/{resource_id}", response_model=ResourceOut)
def update_resource(
    resource_id: str, body: ResourceUpdate, request: Request
) -> ResourceOut:
    updated = _service(request).update_resource(
        resource_id,
        body.name,
        body.status,
        body.expected_version,
    )
    return _to_out(updated)
