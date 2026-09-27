# 可持久化数据访问层（Python / SQLite）

用本地 SQLite 文件承载资源状态，在服务层（FastAPI HTTP 入口）与直接调用
（`ResourceService`）两类入口下，为并发写入、事务回滚与游标分页提供
一致且可解释的结果。

## 目录结构

```
app/
  connection.py    # SQLite 连接/WAL、建表与旧结构迁移、底层异常翻译
  errors.py        # 对外错误类型（冲突原因可区分）
  models.py        # Resource / Create / Update / Query / Page / PurgeResult 类型
  cursor.py        # 不透明游标的编码与校验
  repository.py    # 全部 SQL：插入、乐观锁更新、软删/恢复、分批清理、keyset 分页
  transaction.py   # 请求级事务边界（BEGIN IMMEDIATE/COMMIT/ROLLBACK）
  service.py       # 编排入口：事务 + 仓储 + ID/时间戳/时钟 + 日志
  http_api.py      # FastAPI 应用与错误 -> HTTP 状态码映射
main.py            # 进程启动入口（初始化 DB，挂载 app）
tests/             # 乐观锁 / 事务 / 分页 / 生命周期 / 清理 / 迁移 / HTTP
```

## 数据模型

表 `resources`：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT PRIMARY KEY | 服务层生成的 UUID hex，也可由调用方指定 |
| `name` | TEXT NOT NULL | 资源名；唯一性只对 `active` 行生效（见下） |
| `payload` | TEXT | 资源内容，默认空串 |
| `version` | INTEGER NOT NULL DEFAULT 1 | 乐观锁版本号，每次成功更新/删除/恢复 +1 |
| `created_at` | INTEGER NOT NULL | 创建时间（毫秒），分页排序键第一列 |
| `updated_at` | INTEGER NOT NULL | 最近更新时间（毫秒） |
| `status` | TEXT NOT NULL DEFAULT 'active' | 生命周期状态：`active` / `deleted` |
| `deleted_at` | INTEGER | 进入回收站的毫秒时间；正常资源为 NULL，恢复时清空 |

唯一约束：`CREATE UNIQUE INDEX ux_resources_active_name ON resources(name) WHERE status = 'active'`。
即**只有正常状态的资源占用名字**，回收站里的行不占名。对早期建表（`name` 上为
表级 UNIQUE、无 `status`/`deleted_at` 列）的库，`Database.initialize()` 会做
一次性在线迁移：整表搬运到新结构、旧数据全部视为 `active`、版本号原样保留，
迁移包在一个事务里且幂等可重复执行。

## 资源生命周期（回收站）

删除是**软删除**：资源从正常视图移入回收站、记录 `deleted_at`、版本号 +1，
之后可以带版本号恢复；回收站中超过保留期的资源可被物理清理。

### 删除 / 恢复的并发保护

删除和恢复都必须携带资源**当前版本号** `expected_version`，SQL 以
`UPDATE ... WHERE id=? AND version=? AND status=?` 原子完成，成功时
`version = version + 1`。三类拒绝原因用不同的 `code` 明确区分：

| 情况 | code | HTTP | 说明 |
| --- | --- | --- | --- |
| 物理不存在 / 已被清理 | `resource_not_found` | 404 | 根本没有这个资源 |
| 版本号落后 | `version_conflict` | 409 | 携带 `expected_version` 与当前 `current_version`，拒绝写入、不覆盖他人改动 |
| 当前状态不允许该操作 | `invalid_state` | 409 | 如对已删除资源再删、对正常资源发恢复；携带 `current_status` |
| 恢复时名字已被占用 | `unique_constraint_violation` | 409 | 与版本冲突是不同 code |

归因顺序：**存在性 → 当前状态 → 版本号**。即请求发起时资源状态就不适合
（对 `active` 发恢复、对 `deleted` 发删除）得到 `invalid_state`；状态本身
合适、只是版本落后（这期间被别人更新/删除/恢复过）得到 `version_conflict`，
调用方应重新读取最新版本与状态后再决定是否重试。

### 默认可见性与版本演进

- 回收站资源在**默认单查**（`GET /resources/{id}`）和**默认列表**
  （`status=active`）中视为不存在，对其发更新同样得到 `resource_not_found`；
  需要查看回收站记录可用 `include_deleted=true`（单查）或 `status=deleted`/
  `status=all`（列表）。
- 恢复后资源重新出现在默认视图，`deleted_at` 清空，**版本号继续单调递增**，
  不回退、不重复（例：建 v1 → 删 v2 → 恢复 v3 → 更新 v4）。

### 名字复用规则（明确且一致）

- 删除即释放名字：资源删除后，其 `name` 可被新资源创建使用（数据库层面由
  partial unique index 保证，不靠应用层先查后判）。
- 回收站中可以同时存在多个同名的已删除行。
- 恢复时若该名字当前已被某个 `active` 资源占用，恢复被拒绝
  （`unique_constraint_violation`），旧资源**原样留在回收站**，绝不产生两个
  同名正常资源、也不覆盖占用者；待占用者自己也被删除（名字再次释放）后才能
  恢复成功。

### 保留期清理（物理清除）

`ResourceService.purge_deleted(retention_ms?, batch_size?, max_batches?)`：

- 只删除满足 `status='deleted' AND deleted_at <= now - retention_ms` 的行；
  保留期内、已恢复（`deleted_at=NULL`）或被重新删除（`deleted_at` 刷新）的
  行都不会被误清。`retention_ms=0` 表示立即清除。
- 默认保留 7 天（构造服务时可用 `default_retention_ms` 配置），默认批大小
  500（`purge_batch_size` 配置），调用时均可覆盖。
- **分批推进**：每批在自己的短事务里提交（先按
  `(deleted_at ASC, id ASC)` 取一批候选 id，再按 id 删除），批与批之间不持
  写锁，不会长时间卡住正常读写；`max_batches` 可限制单次调用的工作量，返回
  的 `has_more` 提示是否还有积压，调用方稍后继续调用即可。
- **失败重试结果一致**：某批失败只回滚该批，已提交的前批不会被重复处理；
  重试时到期集合按当前时间重新计算，删除是幂等的“清到期行”，不会重复清理
  或把状态搞乱。
- 结果用 `PurgeResult` 解释：`purged`（清除数）、`scanned`（候选数）、
  `batches`（实际批数）、`has_more`（是否还有积压）；底层存储异常仍按统一
  规则翻译，不直接抛出。

## 并发与乐观锁规则

- 更新必须携带 `expected_version`。SQL 以
  `UPDATE ... WHERE id = ? AND version = ?` 原子地完成条件更新，
  成功时 `version = version + 1`。
- 资源物理不存在或已在回收站 → `resource_not_found`（HTTP 404）。
- 影响行数为 0（版本已被他人推进）→ `version_conflict`（HTTP 409），
  错误体携带 `expected_version` 与数据库当前 `current_version`，
  调用方可据此读取最新数据后重试；**拒绝写入，绝不覆盖他人提交**。
- 名字撞唯一约束（仅 `active` 行）→ `unique_constraint_violation`（HTTP 409），
  与乐观锁冲突是不同的 `code`，原因可区分。
- 文件库启用 WAL，写事务以 `BEGIN IMMEDIATE` 在开始时取得保留锁，
  写事务之间串行，锁等待由 `busy_timeout=5000ms` 兜底。因此两个并发的
  删除/恢复竞争同一行时恰有一个成功，另一个拿到锁后读到的已是最新状态。

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
- **状态筛选与游标兼容**：列表支持 `status=active`（默认）/`deleted`/`all`，
  筛选只改变可见行集合，排序键与游标编码不变。游标只表示排序位置、不绑定
  筛选条件，因此跨筛选复用同一个游标不会被判非法，而是按新的状态集合从同一
  位置继续（结果确定）。
- 相比 OFFSET 分页，游标表示“排序位置”而非行号，翻页过程中增删数据时：
  - **非法游标**（空串、不可解码、版本不符、字段缺失/类型非法）→
    `invalid_cursor`（HTTP 400），明确拒绝；
  - 游标指向的锚点行在翻页期间被删除或物理清理：位置仍有效，不跳过其他行，
    翻页正常继续（锚点行若此前已被读到，保留在读结果中）；
  - 位置之后的行被删除/清理或被恢复（移出当前筛选集合）：尚未读到的自然
    不再出现；仍在集合中的行不重复、不遗漏；
  - 位置之后有行因恢复/新增进入当前筛选集合：会在后续页被读到，且恰好一次；
  - 位置之前的集合变动：本轮遍历**不回溯**，下一轮从头遍历可见；
  - 游标位于所有数据之后：返回空页且 `has_more=false`，遍历总是
    以 `has_more=false` 明确终止。
- `page_size` 限制在 1..100；`status` 取非法值 → `invalid_input`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources` | 创建，body `{name, payload?, id?}` |
| GET | `/resources/{id}` | 单查（默认不含回收站） |
| PUT | `/resources/{id}` | 乐观锁更新，body `{expected_version, name?, payload?}` |
| DELETE | `/resources/{id}` | 软删除进回收站，body `{expected_version}` |
| POST | `/resources/{id}/restore` | 从回收站恢复，body `{expected_version}` |
| POST | `/resources/purge` | 按保留期分批清理，body `{retention_ms?, batch_size?, max_batches?}` |
| GET | `/resources?status=&page_size=&cursor=&name_contains=` | 游标分页，`status` 取 `active`/`deleted`/`all` |

错误响应统一为 `{"error": {"code", "message", ...}}`；`version_conflict`
额外携带 `resource_id/expected_version/current_version`，`invalid_state`
额外携带 `resource_id/current_status`。

> 注：直接使用 `ResourceService` 时，单查可传 `include_deleted=True` 查看
> 回收站记录；删除/恢复入参为 `ResourceStateChange(id, expected_version)`。

## 本地验证

```bash
# 1. 安装依赖（需要 Python >= 3.14）
uv sync

# 2. 运行全部单测（caplog 会输出版本号 / 事务结果 / 游标位置 / 清理批次）
uv run pytest

# 查看生命周期、并发与清理日志
uv run pytest tests/test_lifecycle.py tests/test_purge.py \
  -o log_cli=true -o log_cli_level=INFO

# 3. 启动服务（默认在工作目录生成 data.db；旧结构库启动时自动迁移）
DAL_DB_PATH=/tmp/dal.db uv run python main.py
```

用 curl 走一遍删除 → 回收站查看 → 恢复 / 清理流程：

```bash
BASE=localhost:8000
# 创建并拿到 id 与版本号
ID=$(curl -s -X POST $BASE/resources -H 'content-type: application/json' \
  -d '{"name":"demo","payload":"v0"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 软删除（携带版本 1）
curl -s -X DELETE $BASE/resources/$ID -H 'content-type: application/json' \
  -d '{"expected_version":1}'
# 默认列表/单查看不到；回收站里能看到
curl -s "$BASE/resources?status=deleted" | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' $BASE/resources/$ID   # 404

# 恢复（携带删除返回的版本 2）；若名字已被他人占用会得到 409
curl -s -X POST $BASE/resources/$ID/restore -H 'content-type: application/json' \
  -d '{"expected_version":2}'

# 再次删除后，按保留期清理（retention_ms=0 立即清；也可只清一批看 has_more）
curl -s -X DELETE $BASE/resources/$ID -H 'content-type: application/json' \
  -d '{"expected_version":3}'
curl -s -X POST $BASE/resources/purge -H 'content-type: application/json' \
  -d '{"retention_ms":0,"batch_size":100}'
```

测试说明：

- `tests/test_optimistic_lock.py`：成功更新、过期版本拒绝（含期望/当前版本号）、
  两线程并发竞争恰好一成一冲突、资源不存在分类。
- `tests/test_transaction.py`：批量提交、约束失败整体回滚无残留、
  显式异常回滚、底层 `sqlite3` 异常被包装不外泄。
- `tests/test_pagination.py`：全量翻页无重无漏、同一游标结果稳定、
  非法/过期游标拒绝或终止、翻页中删除与插入的语义。
- `tests/test_lifecycle.py`：删除隐藏/恢复可见、版本单调递增、三类错误可区分、
  更新/删除/恢复并发撞车、名字释放复用与恢复撞名。
- `tests/test_lifecycle_pagination.py`：带 `status` 筛选翻页期间发生删除、
  恢复、物理清理与新增时不重不漏，游标跨筛选复用与非法状态拒绝。
- `tests/test_purge.py`：到期判定、保留期内不误清、active/已恢复不清、
  分批推进、中途失败重试结果一致、参数校验与结果可解释。
- `tests/test_migration.py`：旧结构库迁移保数据、迁移后生命周期可用、重复初始化幂等。
- `tests/test_http_api.py`：上述错误分类与生命周期/清理端点在 HTTP 入口下的
  状态码与响应体。
