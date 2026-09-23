# 可持久化数据访问层（Python / SQLite）

用本地 SQLite 文件承载资源状态，在服务层（FastAPI HTTP 入口）与直接调用
（`ResourceService`）两类入口下，为并发写入、事务回滚与游标分页提供
一致且可解释的结果。

## 目录结构

```
app/
  connection.py    # SQLite 连接/WAL、建表、底层异常翻译
  errors.py        # 对外错误类型（冲突原因可区分）
  models.py        # Resource / Create / Update / Query / Page 类型
  cursor.py        # 不透明游标的编码与校验
  repository.py    # 全部 SQL：插入、乐观锁更新、keyset 分页
  transaction.py   # 请求级事务边界（BEGIN IMMEDIATE/COMMIT/ROLLBACK）
  service.py       # 编排入口：事务 + 仓储 + ID/时间戳 + 日志
  http_api.py      # FastAPI 应用与错误 -> HTTP 状态码映射
main.py            # 进程启动入口（初始化 DB，挂载 app）
tests/             # 乐观锁 / 事务 / 分页 / HTTP 四组测试
```

## 数据模型

表 `resources`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PRIMARY KEY | 服务层生成的 UUID hex，也可由调用方指定 |
| `name` | TEXT NOT NULL UNIQUE | 资源名，唯一约束由数据库保证 |
| `payload` | TEXT | 资源内容，默认空串 |
| `version` | INTEGER NOT NULL DEFAULT 1 | 乐观锁版本号，每次成功更新 +1 |
| `created_at` | INTEGER NOT NULL | 创建时间（毫秒），分页排序键第一列 |
| `updated_at` | INTEGER NOT NULL | 最近更新时间（毫秒） |

## 并发与乐观锁规则

- 更新必须携带 `expected_version`。SQL 以
  `UPDATE ... WHERE id = ? AND version = ?` 原子地完成条件更新，
  成功时 `version = version + 1`。
- 资源不存在 → `resource_not_found`（HTTP 404）。
- 影响行数为 0（版本已被他人推进）→ `version_conflict`（HTTP 409），
  错误体携带 `expected_version` 与数据库当前 `current_version`，
  调用方可据此读取最新数据后重试；**拒绝写入，绝不覆盖他人提交**。
- 名字撞唯一约束 → `unique_constraint_violation`（HTTP 409），
  与乐观锁冲突是不同的 `code`，原因可区分。
- 文件库启用 WAL，写事务以 `BEGIN IMMEDIATE` 在开始时取得保留锁，
  写事务之间串行，锁等待由 `busy_timeout=5000ms` 兜底。

## 事务规则

- 每个写请求（创建、更新、批量写入）都运行在一个 `transactional` 边界内：
  正常离开 `with` 块 → `COMMIT`；任何异常 → `ROLLBACK` 后再抛出，
  多步写入要么全部落库、要么不留任何部分数据。
- 已是数据访问层错误（冲突、约束、非法输入等）时，回滚后**原样抛出以保持分类**；
  `sqlite3.Error` 被翻译为 `storage_error`；意外异常包装为 `transaction_failed`。
  底层存储异常的原始类型不会透出到对外接口。
- 读请求使用短事务（`BEGIN IMMEDIATE` 保证单次查询有一致性快照后立即结束）。

## 分页语义

- 固定、稳定的排序键：`ORDER BY created_at ASC, id ASC`（`id` 作为
  同毫秒并列时的确定性 tiebreaker）。
- 采用 keyset 分页：游标是不透明字符串（`base64url(JSON)`，内含版本号、
  上一页最后一条的 `created_at` 与 `id`），查询条件为
  `(created_at, id) > (?, ?)`，每页取 `page_size+1` 行判断 `has_more`。
- 相比 OFFSET 分页，游标表示“排序位置”而非行号，翻页过程中增删数据时：
  - **非法游标**（空串、不可解码、版本不符、字段缺失/类型非法）→
    `invalid_cursor`（HTTP 400），明确拒绝；
  - 游标指向的锚点行在翻页期间被删除：位置仍有效，不跳过其他行，
    翻页正常继续（锚点行若此前已被读到，保留在读结果中）；
  - 位置之后的行被删除：自然不再出现；仍存在的行不重复、不遗漏；
  - 位置之后新插入的行：会在后续页被读到，且恰好一次；
  - 位置之前新插入的行：本轮遍历**不回溯**，下一轮从头遍历可见；
  - 游标位于所有数据之后：返回空页且 `has_more=false`，遍历总是
    以 `has_more=false` 明确终止。
- `page_size` 限制在 1..100。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources` | 创建，body `{name, payload?, id?}` |
| GET | `/resources/{id}` | 单查 |
| PUT | `/resources/{id}` | 乐观锁更新，body `{expected_version, name?, payload?}` |
| GET | `/resources?page_size=&cursor=&name_contains=` | 游标分页 |

错误响应统一为 `{"error": {"code", "message", ...}}`。

## 本地验证

```bash
# 1. 安装依赖（需要 Python >= 3.14）
uv sync

# 2. 运行全部单测（24 个用例；caplog 会输出版本号 / 事务结果 / 游标位置）
uv run pytest

# 查看并发与事务日志
uv run pytest tests/test_optimistic_lock.py tests/test_transaction.py \
  -o log_cli=true -o log_cli_level=INFO

# 3. 启动服务（默认在工作目录生成 data.db）
DAL_DB_PATH=/tmp/dal.db uv run python main.py
# 另开终端：
curl -s -X POST localhost:8000/resources -H 'content-type: application/json' \
  -d '{"name":"demo","payload":"v0"}'
curl -s 'localhost:8000/resources?page_size=10'
```

测试说明：

- `tests/test_optimistic_lock.py`：成功更新、过期版本拒绝（含期望/当前版本号）、
  两线程并发竞争恰好一成一冲突、资源不存在分类。
- `tests/test_transaction.py`：批量提交、约束失败整体回滚无残留、
  显式异常回滚、底层 `sqlite3` 异常被包装不外泄。
- `tests/test_pagination.py`：全量翻页无重无漏、同一游标结果稳定、
  非法/过期游标拒绝或终止、翻页中删除与插入的语义。
- `tests/test_http_api.py`：上述错误分类在 HTTP 入口下的状态码与响应体。
