# 数据访问层服务（Data Access Layer）

基于 **FastAPI + SQLite(WAL)** 的可持久化数据访问层，提供资源的新增、更新、条件查询，
具备请求级事务边界、基于版本号的乐观锁，以及基于游标的稳定分页。
所有底层存储异常都会被转译为对外可解释的错误类型，不原样透出。

## 1. 目录结构

```
app/
  errors.py        # 对外错误类型体系（错误归类与可区分 reason）
  models.py        # 领域模型：Resource、Page[T]
  database.py      # SQLite 连接、WAL/忙等待配置、建表
  pagination.py    # 游标编解码、limit 约束、排序键
  repositories.py  # ResourceRepository：CRUD + 乐观锁 + 键集分页
  unit_of_work.py  # UnitOfWork：请求级事务边界（BEGIN IMMEDIATE/COMMIT/ROLLBACK）
  services.py      # ResourceService：事务编排，服务层错误转译兜底
  schemas.py       # HTTP 入参/出参 Pydantic 模型
  api.py           # FastAPI 路由
  main.py          # 应用装配、lifespan 建表、统一异常处理
tests/             # 并发更新、事务回滚、分页稳定性、HTTP 归类测试
```

## 2. 数据模型

`resources` 表：

| 列          | 类型    | 说明                                                        |
|-------------|---------|-------------------------------------------------------------|
| `id`        | TEXT PK| UUID4 hex，服务端生成                                       |
| `name`      | TEXT    | 资源名称（1~200 字符，支持子串查询，LIKE 通配符已转义）      |
| `status`    | TEXT    | 资源状态（1~50 字符，支持精确过滤）                          |
| `version`   | INTEGER | 乐观锁版本号，从 **1** 开始，每次成功更新自增 1，不复用       |
| `created_at`| TEXT    | 创建时间，UTC ISO-8601（微秒、固定 `+00:00`）                |
| `updated_at`| TEXT    | 最近更新时间，同构于 `created_at`                            |

索引：`(created_at, id)`、`(status, created_at, id)`，分别服务于默认排序与状态过滤分页。

## 3. 事务规则

- 一个写请求对应一个 `UnitOfWork`（一个连接、一个事务）。
- 进入事务执行 **`BEGIN IMMEDIATE`**：立即取得 SQLite 保留锁，写入被串行化，
  并杜绝脏读；多步写入之间看到的是同一事务内的一致视图。
- `commit()` 成功才全部生效；以下情况**整体回滚**，不残留部分数据：
  - `with` 块内抛出任何异常（`__exit__` 自动 `ROLLBACK` 后再向外抛）；
  - 显式调用 `rollback()`；
  - `COMMIT` 阶段连接故障（回滚并转译为 `storage_error`）。
- 读操作使用独立只读连接，在 WAL 模式下不阻塞写。
- 服务层边界提供兜底：任何漏网的 `sqlite3.Error` 一律转译为 `StorageError`，
  对外错误文案不含驱动原始信息。

## 4. 并发与乐观锁

更新必须携带 `expected_version`。SQL 为条件更新：

```sql
UPDATE resources SET name=?, status=?, version=version+1, updated_at=?
WHERE id=? AND version=?
```

- 影响 1 行：更新成功，返回新版本。
- 影响 0 行时再读当前版本并区分原因：
  - 记录不存在 → `not_found`（404）；
  - 当前版本 ≠ 期望版本 → `version_conflict`（409），响应中携带
    **expected 与 actual 版本号**，客户端据此可决定重读后重试。
- 由于写事务经 `BEGIN IMMEDIATE` 串行化，两个并发更新中先提交者把版本推进，
  后到者以新版本重新求值，条件不再匹配而被拒绝，**绝不会覆盖他人已提交的变更**
  （见 `tests/test_optimistic_lock.py` 的真实双线程测试）。

### 错误归类

| 异常                    | HTTP | reason               | 含义                         |
|-------------------------|------|----------------------|------------------------------|
| `NotFoundError`         | 404  | `not_found`          | 资源不存在/已删除             |
| `VersionConflictError`  | 409  | `version_conflict`   | 乐观锁版本冲突（含版本号）    |
| `ConflictError`         | 409  | `multi_step_aborted` 等 | 其它可区分的状态冲突       |
| `InvalidCursorError`    | 400  | `invalid_cursor`     | 游标非法/排序键不可比         |
| `StorageError`          | 500  | `storage_error`      | 存储故障（隐藏底层细节）       |
| 未知异常                | 500  | `internal_error`     | 兜底                          |

## 5. 分页语义

- 排序键固定为 **`(created_at ASC, id ASC)`**：时间戳采用定长 UTC ISO-8601，
  字符串字典序即时间序；`id` 作为同刻（tie-breaker）主键，保证**全序、稳定、可解释**。
- 游标为不透明字符串：`base64url(JSON{created_at,id})`，仅由服务端产生/解析。
- 查询采用**键集（keyset）谓词**而非 OFFSET：
  `(created_at > ?) OR (created_at = ? AND id > ?)`，每次取 `limit+1` 行判定是否有下一页。
- 终止语义：末页 `has_more=false` 且 `next_cursor=""`；调用方见到空游标即应停止翻页。
- 翻页过程中数据增删的行为（**明确语义，不重不漏**）：
  - **删除**游标之后的记录：直接从结果集中消失，后续页不出现已删行、不报错；
  - **新增**记录：`created_at` 晚于游标则在后续页可见；本遍历不会回头插入到更早的页，
    因此**不会重复**也不会打乱既有顺序。
- **拒绝语义**：游标出现下列任一情况返回 400 `invalid_cursor`，绝不“宽容跳过”：
  非 base64、非 JSON、不是对象、缺少/为空 `created_at` 或 `id`、
  `created_at` 不是可比较的 UTC ISO-8601。
- `limit` 约束在 `[1, 100]`（超出由服务端收敛到边界）。

## 6. HTTP 接口

| 方法 | 路径                    | 说明                                        |
|------|-------------------------|---------------------------------------------|
| POST | `/resources`            | 新增，body：`{name,status}`，返回 version=1 |
| GET  | `/resources/{id}`       | 单条读取                                    |
| PUT  | `/resources/{id}`       | 乐观更新，body：`{name,status,expected_version}` |
| GET  | `/resources`            | 分页：`limit,cursor,status,name_contains`   |

分页响应：`{items:[...], next_cursor:"", has_more:false}`。
错误响应统一为：`{error, reason, detail}`。

启动服务：

```bash
uv run uvicorn app.main:app --reload
# 交互文档：http://127.0.0.1:8000/docs
```

数据库路径默认 `./data.db`，可用环境变量 `DAL_DB_PATH` 覆盖（测试自动使用临时库）。

## 7. 本地验证

```bash
uv sync                                  # 安装含 pytest/httpx 的依赖
uv run pytest                            # 运行全部测试
uv run pytest -s                         # 查看单测日志（版本号/事务结果/游标位置）
uv run pytest tests/test_optimistic_lock.py -s   # 只看并发更新
uv run pytest tests/test_transactions.py -s      # 只看事务回滚
uv run python -m compileall app tests            # 语法编译检查
uv run uvicorn app.main:app                      # 手动起服务
```

测试日志示例（`-s`）：

```
CONCURRENT UPDATE result=COMMITTED name=from-t2 version=2
CONCURRENT UPDATE result=REJECTED reason=version_conflict expected=1 actual=2
TRANSACTION result=ROLLED_BACK reason=multi_step_aborted
POST-ROLLBACK rows={'baseline'} baseline_version=1
PAGE n=1 size=10 has_more=True cursor=eyJjcmVhdGVkX2F0
PAGE n=3 size=5 has_more=False cursor=<END>
PAGINATION result=COMPLETE total=25 duplicates=0
```

### 手动 curl 验证乐观锁

```bash
curl -s -X POST localhost:8000/resources -H 'content-type: application/json' \
  -d '{"name":"a","status":"active"}'
# -> {"id":"...","version":1,...}
curl -s -X PUT localhost:8000/resources/<id> -H 'content-type: application/json' \
  -d '{"name":"a2","status":"active","expected_version":1}'   # 200 version=2
curl -s -X PUT localhost:8000/resources/<id> -H 'content-type: application/json' \
  -d '{"name":"a3","status":"active","expected_version":1}'   # 409 version_conflict(actual=2)
```
