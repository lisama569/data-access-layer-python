"""游标编解码与键集(keyset)分页谓词。

游标内容为排序键 (created_at, id)，使用 base64url(JSON) 编码，
对调用方不透明；任何损坏/篡改都会被拒绝为 InvalidCursorError。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from app.errors import InvalidCursorError

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Cursor:
    """游标承载的排序键快照。"""

    created_at: str
    id: str


def encode_cursor(created_at: str, resource_id: str) -> str:
    """把排序键编码为不透明游标字符串。"""
    payload = json.dumps(
        {"created_at": created_at, "id": resource_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> Cursor:
    """解码并严格校验；任何异常都归为 InvalidCursorError。

    拒绝情形（reason 可区分）：
    - 空串由调用方视作"首页"，不会进入本函数；
    - 非 base64 / 非 JSON / 缺字段 / 类型错误 -> malformed_cursor；
    - 时间戳不是合法 UTC ISO-8601 -> non_comparable_key。
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("malformed cursor token") from exc

    if not isinstance(data, dict):
        raise InvalidCursorError("cursor payload must be an object")

    created_at = data.get("created_at")
    resource_id = data.get("id")
    if not isinstance(created_at, str) or not isinstance(resource_id, str):
        raise InvalidCursorError("cursor must contain string 'created_at' and 'id'")
    if not created_at or not resource_id:
        raise InvalidCursorError("cursor fields must be non-empty")

    # 校验 created_at 是可比较的 UTC ISO-8601 时间戳，保证排序键语义稳定。
    try:
        _parse_utc(created_at)
    except ValueError as exc:
        raise InvalidCursorError("cursor created_at is not comparable ISO-8601") from exc

    return Cursor(created_at=created_at, id=resource_id)


def _parse_utc(value: str):
    from datetime import datetime

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt


def clamp_limit(limit: int) -> int:
    """将入参 limit 约束到 [1, MAX_LIMIT]。"""
    if limit < 1:
        return 1
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit
