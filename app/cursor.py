"""游标编码 / 解码。

分页排序键固定为 ``(created_at ASC, id ASC)``，因此游标只需记录上一页
最后一条记录的排序键值。游标为不透明字符串（base64url(JSON)），调用方
不应自行构造；任何格式损坏、字段缺失或类型不符都被视为非法游标。
"""
from __future__ import annotations

import base64
import json

from app.errors import InvalidCursorError

_CURSOR_VERSION = 1


def encode_cursor(created_at: int, resource_id: str) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "ts": int(created_at), "id": str(resource_id)},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> tuple[int, str]:
    """返回 ``(created_at, id)``；游标非法时抛 :class:`InvalidCursorError`。"""
    if not isinstance(token, str) or not token:
        raise InvalidCursorError("游标为空")
    raw = token.encode("ascii", errors="ignore")
    padded = raw + b"=" * (-len(raw) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:  # binascii / UnicodeDecodeError / JSON 错误统一归类
        raise InvalidCursorError("游标无法解码", cause=exc) from exc

    if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
        raise InvalidCursorError("游标版本不受支持")
    ts = data.get("ts")
    resource_id = data.get("id")
    # bool 是 int 的子类，显式排除，避免 True/False 伪装时间戳
    if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        raise InvalidCursorError("游标中的排序键时间戳非法")
    if not isinstance(resource_id, str) or not resource_id:
        raise InvalidCursorError("游标中的资源标识非法")
    return ts, resource_id
