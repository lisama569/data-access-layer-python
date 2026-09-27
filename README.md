# 可持久化数据访问层（Python / SQLite）

用本地 SQLite 文件承载资源状态，在服务层（FastAPI HTTP 入口）与直接调用
（`ResourceService`）两类入口下，为并发写入、事务回滚与游标分页提供
一致且可解释的结果。

## 目录结构

```
app/
  connection.py    # SQLite 连接/WAL、建表与幂等迁移、底层异常翻译
  errors.py        # 对外错误类型（不存在/版本冲突/状态冲突/约束/非法游标可区分）
  models.py        # Resource / Create / Update / Delete / Restore / Query / Page / PurgeResult
  cursor.py        # 不透明游标的编码与校验
  repository.py    # 全部 SQL：插入、乐观锁更新、软删/恢复、keyset 分页、分批清理
  transaction.py   # 请求级事务边界（BEGIN IMMEDIATE/COMMIT/ROLLBACK）
  service.py       # 编排入口：事务 + 仓储 + ID/时间戳/时钟 + 回收站清理 + 日志
  http_api.py      # FastAPI 应用与错误 -> HTTP 状态码映射
main.py            # 进程启动入口（初始化 DB，挂载 app）
tests/             # 乐观锁 / 事务 / 分页 / 生命周期 / 状态筛选分页 / 清理 / HTTP
```

## 数据模型

表 `resources`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PRIMARY KEY | 服务层生成的 UUID hex，也可由调用方指定 |
| `name` | TEXT NOT NULL UNIQUE | 资源名，**跨状态全局唯一**（含回收站），约束由数据库保证 |
| `payload` | TEXT | 资源内容，默认空串 |
| `version` | INTEGER NOT NULL DEFAULT 1 | 乐观锁版本号，更新/删除/恢复每成功一次 +1 |
| `created_at` | INTEGER NOT NULL | 创建时间（毫秒），分页排序键第一列，软删/恢复不变 |
| `updated_at` | INTEGER NOT NULL | 最近状态变更时间（毫秒） |
| `status` | TEXT NOT NULL DEFAULT 'active' | 生命周期状态：`active` / `deleted` |
| `deleted_at` | INTEGER | 进入回收站的毫秒时间戳；恢复时清空，清理时作为保留期判据 |

> 老库平滑升级：`initialize()` 会幂等地补列（`status` 默认 `active`）并创建
> 回收站部分索引 `idx_resources_deleted_at`，已有数据无需手工迁移。

## 资源生命周期（回收站）

资源有两个真实状态：`active`（正常）与 `deleted`（在回收站）。删除是**软删除**，
之后可恢复；只有按保留期清理才会物理消失。

- **删除 / 恢复都携带 `expected_version`**，SQL 以
  `UPDATE ... WHERE id=? AND version=? AND status=?` 原子完成“状态+版本”双条件更新，
  成功时 `version = version + 1`。
- 拒绝原因分三类，**绝不覆盖他人提交**（判定顺序：先存在性、再版本、后状态）：
  - 资源不存在（含已被物理清理）→ `resource_not_found`（HTTP 404）；
  - 版本不匹配（期间被他人改过、删过或恢复过）→ `version_conflict`（HTTP 409），
    携带 `expected_version` / `current_version`，调用方应重读最新数据后重试；
  - 版本匹配但当前状态不允许（如重复删除、恢复一个正常资源、更新回收站资源）
    → `resource_state_conflict`（HTTP 409），携带 `current_state`。
- **可见性**：回收站资源在默认单查（`get_resource`）与默认列表
  （`status=active`）中视为不存在；可用 `get_resource(..., include_deleted=True)`
  或列表 `status=deleted` / `status=all` 查看；恢复后重新可读。
- **版本号单调向前**：删除、恢复各让版本 +1，不回退、不重复；恢复不会得到旧版本号。
- **名字规则（明确且一致）**：`name` 的唯一约束覆盖回收站记录，即
  **软删期间名字仍被保留**，其它资源不能复用；恢复因此永远不会撞名，
  也不会与已有数据冲突；只有资源被**物理清理**后，名字才释放给新资源使用。
- 恢复仅把状态改回 `active`、清空 `deleted_at`，`created_at` 保持不变，
  因此它在分页排序中的位置由创建时间决定（语义稳定、可解释）。

## 列表状态筛选与游标

- 列表新增 `status` 参数：`active`（默认）/ `deleted`（回收站）/ `all`（不过滤）。
  非法值 → `invalid_input`（HTTP 400）。
- 排序键仍是固定的 `(created_at ASC, id ASC)`，游标**只表示排序位置、与筛选无关**：
  同一游标配合不同 `status`，含义是“该筛选集合中、此位置之后”的结果，各自稳定可重复。
- 翻页过程中发生 删除（移入回收站）/ 恢复 / 新增 / 物理清理 时，沿用 keyset 语义：
  被移出当前筛选集合的行（删除、被清掉）不再出现；被移入的行（恢复、位置之后新增）
  在后续页恰好读到一次；锚点行消失不跳过其它行；始终无重复、无遗漏，
  并以 `has_more=false` 干净终止。
- **非法游标**（空串、不可解码、版本不符、字段缺失/类型非法）→
  `invalid_cursor`（HTTP 400），明确拒绝；**游标越过末尾**（包括该筛选下已无数据）
  返回空页且 `has_more=false`，作为清楚的结束信号，不返回错乱数据。

## 按保留期清理（回收站物理清除）

- `purge_deleted(retention_ms=None, batch_size=100, now_ms=None)` 物理删除
  `deleted_at + retention_ms < 当前时间` 的回收站记录（**严格小于**：恰好到点保留，
  超过一毫秒才清），保留期可配置；服务默认保留期 7 天，构造服务时可改。
- **分批推进**：每批一个独立短事务（`BEGIN IMMEDIATE` + 提交后即释放写锁），
  默认每批 100 行，不会长时间持锁阻塞正常读写；批内固定按
  `(deleted_at ASC, id ASC)` 删除，顺序确定。
- **失败重试结果一致**：删除条件只认状态与截止时间，已清掉的行不再匹配，
  重试不重复清理、不误伤保留期内数据、不把状态搞乱（可重入）。
- 返回 `PurgeResult`（`purged_count` / `batch_count` / `cutoff_ms` /
  `retention_ms` / `has_more`），只含可解释计数，不暴露 SQL/连接等存储细节。
- 时钟可注入（`ResourceService(db, clock=...)`、`now_ms` 覆盖），便于本地与测试验证。

## 并发与乐观锁规则

- 写操作（更新、删除、恢复）必须携带 `expected_version`。SQL 以
  `UPDATE ... WHERE id = ? AND version = ? [AND status = ?]` 原子地完成条件更新，
  成功时 `version = version + 1`。
- 资源不存在 → `resource_not_found`（HTTP 404）。
- 影响行数为 0 时按“先版本、后状态”分类：版本已被他人推进（含被他人删除/恢复）
  → `version_conflict`（HTTP 409），错误体携带 `expected_version` 与数据库当前
  `current_version`；版本一致但状态不允许 → `resource_state_conflict`（HTTP 409），
  携带 `current_state`。调用方据此区分“重读重试”还是“换操作”，**拒绝写入，绝不覆盖他人提交**。
- 名字撞唯一约束 → `unique_constraint_violation`（HTTP 409），
  与乐观锁冲突、状态冲突是不同的 `code`，原因可区分。
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
| GET | `/resources/{id}` | 单查（回收站记录默认 404） |
| PUT | `/resources/{id}` | 乐观锁更新，body `{expected_version, name?, payload?}`（仅 active） |
| POST | `/resources/{id}/delete` | 软删除（移入回收站），body `{expected_version}` |
| POST | `/resources/{id}/restore` | 从回收站恢复，body `{expected_version}` |
| GET | `/resources?status=active\|deleted\|all&page_size=&cursor=&name_contains=` | 状态筛选 + 游标分页 |
| POST | `/maintenance/purge-deleted` | 按保留期分批物理清除，body `{retention_ms?, batch_size?}` |

错误响应统一为 `{"error": {"code", "message", ...}}`，409 时按 `code` 区分
`version_conflict` / `resource_state_conflict` / `unique_constraint_violation`。

## 本地验证

```bash
# 1. 安装依赖（需要 Python >= 3.14）
uv sync

# 2. 运行全部单测（63 个用例；caplog 会输出版本号 / 事务结果 / 游标位置 / 清理批次）
uv run pytest

# 只看本轮新增能力
uv run pytest tests/test_lifecycle.py tests/test_status_pagination.py \
  tests/test_retention_purge.py -o log_cli=true -o log_cli_level=INFO

# 3. 启动服务（默认在工作目录生成 data.db）
DAL_DB_PATH=/tmp/dal.db uv run python main.py
# 另开终端：走完 创建 -> 删除 -> 回收站 -> 恢复 -> 清理 全流程
BASE=localhost:8000
ID=$(curl -s -X POST $BASE/resources -H 'content-type: application/json' \
  -d '{"name":"demo","payload":"v0"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST $BASE/resources/$ID/delete  -H 'content-type: application/json' -d '{"expected_version":1}'
curl -s "$BASE/resources/$ID"                       # 默认单查 => 404
curl -s "$BASE/resources?status=deleted&page_size=10"   # 回收站可见
curl -s -X POST $BASE/resources/$ID/restore -H 'content-type: application/json' -d '{"expected_version":2}'
# 再次删除后，用 retention_ms=0 让回收站记录立即超期并物理清除
curl -s -X POST $BASE/resources/$ID/delete  -H 'content-type: application/json' -d '{"expected_version":3}'
curl -s -X POST $BASE/maintenance/purge-deleted -H 'content-type: application/json' \
  -d '{"retention_ms":0,"batch_size":100}'
```

测试说明：

- `tests/test_optimistic_lock.py`：成功更新、过期版本拒绝（含期望/当前版本号）、
  两线程并发竞争恰好一成一冲突、资源不存在分类（原有，未回归）。
- `tests/test_transaction.py`：批量提交、约束失败整体回滚无残留、
  显式异常回滚、底层 `sqlite3` 异常被包装不外泄（原有，未回归）。
- `tests/test_pagination.py`：全量翻页无重无漏、同一游标结果稳定、
  非法/过期游标拒绝或终止、翻页中删除与插入的语义（原有，未回归）。
- `tests/test_lifecycle.py`：删除/恢复与默认隐藏、版本单调、三类拒绝原因、
  更新/恢复的并发撞车、两线程并发删除竞争、名字保留到物理清除后才释放。
- `tests/test_status_pagination.py`：active/deleted/all 筛选互斥、翻页中
  删除/恢复/新增/物理清理不重不漏且顺序稳定、游标跨筛选稳定、非法/越界游标。
- `tests/test_retention_purge.py`：保留期边界（严格小于）、分批计数、
  中途失败重试一致且幂等、不误伤保留期内数据、清理释放名字、结果可解释、入参校验。
- `tests/test_http_api.py`：上述错误分类与生命周期、状态筛选、清理端点在
  HTTP 入口下的状态码与响应体。
