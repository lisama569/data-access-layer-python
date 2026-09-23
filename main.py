"""兼容入口：实际应用位于 app.main。

启动方式：uv run uvicorn app.main:app --reload
"""

from app.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", reload=True)
