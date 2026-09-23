"""HTTP 入参/出参 schema（Pydantic）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=50)


class ResourceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=50)
    expected_version: int = Field(ge=1)


class ResourceOut(BaseModel):
    id: str
    name: str
    status: str
    version: int
    created_at: str
    updated_at: str


class PageOut(BaseModel):
    items: list[ResourceOut]
    next_cursor: str
    has_more: bool


class ErrorOut(BaseModel):
    error: str
    reason: str
    detail: str = ""
