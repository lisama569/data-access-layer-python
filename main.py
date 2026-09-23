"""服务启动入口。"""
from __future__ import annotations

import os

import uvicorn

from app.connection import Database
from app.http_api import create_app

DB_PATH = os.environ.get("DAL_DB_PATH", "data.db")

db = Database(DB_PATH)
db.initialize()
app = create_app(db)

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
