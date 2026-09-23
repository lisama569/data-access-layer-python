"""应用服务层：编排事务边界与仓储操作。

是 HTTP 入口与数据访问层之间的唯一协作面：
- 写操作在单个 UnitOfWork 中完成，多步写入要么全部提交、要么整体回滚；
- 读操作走独立只读连接（WAL 下不阻塞写）；
- 只向上抛出 app.errors 中定义的错误类型。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

from app.database import create_connection, get_db_path, init_schema
from app.errors import DALError, ConflictError, StorageError
from app.models import Page, Resource
from app.repositories import ResourceRepository
from app.unit_of_work import UnitOfWork

_initialized_paths: set[str] = set()


@contextmanager
def _translate_storage() -> Iterator[None]:
    """服务层边界兜底：任何漏网的 sqlite3 异常统一转译为 StorageError。

    已是 DALError 的业务/领域错误原样传播；底层驱动信息不透出。
    注意：with UnitOfWork 的 __exit__ 会在异常传播前先完成回滚。
    """
    try:
        yield
    except DALError:
        raise
    except sqlite3.Error as exc:
        raise StorageError("a storage error occurred") from exc


class ResourceService:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or get_db_path()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        if self.db_path in _initialized_paths:
            return
        conn = create_connection(self.db_path)
        try:
            init_schema(conn)  # isolation_level="" 下立即生效，幂等
        except Exception as exc:  # noqa: BLE001 - 初始化失败统一归类
            raise StorageError("failed to initialize database") from exc
        finally:
            conn.close()
        _initialized_paths.add(self.db_path)

    @contextmanager
    def _read_repo(self) -> Iterator[ResourceRepository]:
        conn = create_connection(self.db_path)
        try:
            yield ResourceRepository(conn)
        finally:
            conn.close()

    # ---- 写 ----

    def create_resource(self, name: str, status: str) -> Resource:
        with _translate_storage():
            with UnitOfWork(self.db_path) as uow:
                resource = uow.resources.create(name, status)
                uow.commit()
        return resource

    def update_resource(
        self,
        resource_id: str,
        name: str,
        status: str,
        expected_version: int,
    ) -> Resource:
        with _translate_storage():
            with UnitOfWork(self.db_path) as uow:
                resource = uow.resources.update(
                    resource_id, name, status, expected_version
                )
                uow.commit()
        return resource

    def create_many(self, items: list[tuple[str, str]]) -> list[Resource]:
        """同一事务内写入多条，全部成功才提交。"""
        with _translate_storage():
            with UnitOfWork(self.db_path) as uow:
                created = [
                    uow.resources.create(name, status) for name, status in items
                ]
                uow.commit()
        return created

    def create_many_then_fail(self, items: list[tuple[str, str]]) -> None:
        """先在同一事务内写入多条，再主动失败：必须整体回滚，无残留数据。"""
        with _translate_storage():
            with UnitOfWork(self.db_path) as uow:
                for name, status in items:
                    uow.resources.create(name, status)
                # 模拟多步操作中的业务失败；__exit__ 会先回滚再把错误抛出。
                raise ConflictError(
                    "multi-step operation aborted before commit",
                    reason="multi_step_aborted",
                )

    # ---- 读 ----

    def get_resource(self, resource_id: str) -> Resource:
        with _translate_storage():
            with self._read_repo() as repo:
                return repo.get(resource_id)

    def list_resources(
        self,
        *,
        limit: int = 20,
        cursor: str = "",
        status: str | None = None,
        name_contains: str | None = None,
    ) -> Page[Resource]:
        with _translate_storage():
            with self._read_repo() as repo:
                return repo.list_page(
                    limit=limit,
                    cursor=cursor,
                    status=status,
                    name_contains=name_contains,
                )
