# PostgreSQL 存储与协议导入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 使用复选框记录实际完成情况，保持当前功能分支。

**Goal:** 将固定协议批次持久化到 PostgreSQL，支持幂等导入、按楼/UID 查询、原子发布和协议再导出。

**Architecture:** 数据库模块独立于来源采集与 HTTP 服务；先冻结并校验输入，再经专用连接执行导入状态机。数据库查询返回解码后的统一记录，复用已有文件导出器。

**Tech Stack:** Python 3.12、PostgreSQL 18、SQLAlchemy 2 Core、psycopg 3、Alembic、pytest。使用 Core 明确管理连接/事务，不引入 ORM 用户实体或 Redis。

**Spec:** [已审阅数据库设计](2026-09-06-postgresql-storage-design.md)、[导出协议 1.0.0](../references/comment-export-protocol-v1.md)。计划不改变协议字段和设计状态机。

## 范围、环境与交付约束

- 本子阶段已完成实现、隔离数据库测试和真实部分批次导入/再导出验收。当前分支 feat/discussion-cache；测试通过不等于目标服务器验证通过。
- Docker CLI 已存在，但本轮只读检查显示 Linux Engine 管道不可用。实施 Task 1 时先确保 Docker Desktop 的 Linux Engine 运行；或使用用户明确指定的专用 PostgreSQL 测试实例。不因数据库不可用用 SQLite 代替集成测试，也不虚报通过。
- 使用独立 compose.postgres.yaml，避免本轮启动或修改已有 web 服务。开发数据库服务名 db，PostgreSQL 18，绑定 127.0.0.1:${POSTGRES_PORT:-5433}:5432，独立命名卷挂载 /var/lib/postgresql。凭据由环境提供，不写入 Git。
- 测试服务独立 profile=test、服务名 db-test，绑定 127.0.0.1:${TEST_POSTGRES_PORT:-55433}:5432；数据用 tmpfs，库名 bilibili_talks_test。TEST_DATABASE_URL 显式传入，DATABASE_URL 绝不作为测试回退。
- 测试初始化只允许 bilibili_talks_test 且连到已选测试实例；每个测试使用独立 schema。清理只操作测试创建的 bt_test_<uuid> schema，不 drop 数据库或删除数据卷。
- 字符存储统一 pg-text-v1，读取/摘要在解码后处理。所有评论 ID/UID 为字符串，state_id 为内部 UUID，不同于 export_id。
- 外部 D:/Data 或 /data 通过 CLI 参数指定，不硬编码。来源 JSONL 只读，不重命名/清理用户的源目录。冻结副本在忽略的 data/import-work 内，每轮结束清理本轮临时目录。
- 不增加网页、模型调用、小时任务调度或采集直写数据库；原运行中的采集进程不停止、不迁移其检查点。

## 文件与接口分工

新增 backend/app/storage/ 包及 __init__.py，JSON 对象沿用现有协议。

| 文件 | 接口及职责 |
| --- | --- |
| errors.py | StorageError(code: str)，仅提供安全错误码 |
| connection.py | open_connection(url: str) 上下文管理器；专用 SQLAlchemy Connection，关闭失败连接，不自动重试事务 |
| schema.py | SQLAlchemy MetaData 及设计中的七张表、生成列、索引、外键 |
| codec.py | encode_text/decode_text(str)->str；encode_json/decode_json(value)->value；无数据库依赖 |
| frozen.py | freeze_batch(input_path: Path, work_root: Path) 上下文管理器，返回 FrozenBatch(directory, manifest, digest) |
| locks.py | video_lock(conn, video_id: str) 上下文管理器；固定命名空间的会话 advisory lock |
| importer.py | import_batch(conn, frozen: FrozenBatch, publish: bool=False)->dict，返回 state_id、receipt_status、published、coverage |
| publication.py | publish_state(conn, video_id: str, state_id: str)->dict，按设计原子切换/回收；独立调用时先取锁 |
| queries.py | read_state(conn, video_id: str, state_id: str|None=None)->dict；list_thread_comments/list_user_comments(conn,state_id,identity,limit,cursor=None)->dict，返回 items/next_cursor |
| exporter.py | export_state(conn, video_id: str, output: Path, state_id: str|None=None)->Path，重复读快照内冻结后复用现有导出器 |
| cli.py | main(argv=None)->int，提供 import/publish/export/query 四个子命令 |
| backend/alembic.ini、backend/migrations/ | Alembic 配置、env.py、script.py.mako 与初始迁移，不保存连接密码 |

## Task 1：数据库连接、迁移及隔离测试环境

**Files:** connection.py、errors.py、schema.py、backend/migrations/versions/0001_discussion_storage.py；compose.postgres.yaml；backend/tests/storage/conftest.py、test_schema.py；更新 pyproject.toml、锁文件、scripts/lock_dependencies.py、.env.example 和 CI。

- [x] 先验证 docker info；不可用时完成纯函数任务，但数据库集成任务保持未验收，明确缺少的运行环境。
- [x] 加入 SQLAlchemy、psycopg[binary]、Alembic 依赖并同步锁定。生成 Alembic 配置时从 DATABASE_URL 读取连接，日志不打印配置值；package/迁移路径使用仓库实际位置。
- [x] 编写 schema 测试，先确认无表时失败：重复 (state_id,comment_id)、同视频第二 working、跨视频状态指针、非法 ID 都拒绝；缺失 parent_id 指向的记录允许保留。

```python
def test_same_video_has_only_one_working_state(db, state_factory):
    state_factory(db, video_id="bilibili:video:1", lifecycle="partial")
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            state_factory(db, video_id="bilibili:video:1", lifecycle="loading")
```

state_factory 是测试 fixture：创建必要视频和状态，不访问真实来源。db 指向每测试新建且已迁移的 schema。

- [x] 初始迁移按 videos（空指针）→ states/子表 → 添加组合指针外键的顺序创建；current/working 不能相同。删除状态级联正文但不删除视频，receipt 引用清理前显式置空。
- [x] 实现 root_rank 与 id_length 生成列、C 排序索引，状态/receipt CHECK 枚举及唯一 working 部分索引。初始迁移 downgrade 只用于独立测试 schema 的回滚测试，禁止在已有用户库试验。
- [x] 启动 `docker compose -f compose.postgres.yaml --profile test up -d db-test`，运行 `python -m pytest backend/tests/storage/test_schema.py -q`；再验证 Alembic upgrade head 重复执行无新更改。
- [x] CI 单独 PostgreSQL service 和 TEST_DATABASE_URL，强制运行 storage 集成测试；本地默认无 TEST_DATABASE_URL 时显式 skip，但不能作为数据库验收证据。
- [x] 验证本任务后提交 `feat: add PostgreSQL schema and migration`。

## Task 2：可逆字符编码与输入冻结

**Files:** codec.py、frozen.py；backend/tests/storage/test_codec.py、test_frozen.py。

- [x] 先写实际 NUL、字面反斜杠加0、反斜杠、代理码点和嵌套键的往返测试；导入缺失时失败后再实现。

```python
@pytest.mark.parametrize("value", ["普通正文", "a\0b", r"a\0b", "\\", "\ud800"])
def test_text_roundtrip(value):
    stored = encode_text(value)
    assert "\0" not in stored
    assert decode_text(stored) == value
```

- [x] 按设计逐字符单次编码/解码，递归处理 JSON 键值；通过 repository 映射边界只编码一次，不让已编码值再次走公共写入入口。无效内部转义返回 storage_decode_error。
- [x] 冻结 current 入口或明确批次目录：复制需要的文件到本轮临时目录，验证完整批次；入口变化导致文件消失时整批重读最多两次，不混合。不调用来源 API，不改变已有文件协议。
- [x] 摘要统一为路径排序后的条目数组，每项 [POSIX相对路径,解析后的JSON值]；JSONL 为记录数组，README 为 ""。以 ensure_ascii=True、sort_keys=True、separators=(",", ":")、allow_nan=False 序列化后 SHA-256。这保留数组/记录顺序，忽略缩进和对象键排列。
- [x] 测试格式空白不改摘要、正文改变改摘要、文件缺失/路径逃逸拒绝、复制后源文件变化不影响冻结输入。协议允许而 PostgreSQL 无法承受的数值/大小由存储入口报 unsupported_storage_value，不截断。
- [x] 运行 `python -m pytest backend/tests/storage/test_codec.py backend/tests/storage/test_frozen.py -q`，提交 `feat: freeze and encode import batches`。

## Task 3：幂等导入、所有权与恢复

**Files:** locks.py、importer.py；backend/tests/storage/test_importer.py、test_import_recovery.py。

- [x] 用双连接测试同视频第二导入返回 import_busy；不同视频独立。锁键按设计固定 SHA-256 转换，锁贯穿整个专用连接，不随着分批事务提交释放。
- [x] 编写重复导入、同 export_id 内容冲突、失败重导测试。使用现有虚构 frozen_case 构造合法文件，禁止直接拿真实数据做测试。

```python
def test_repeated_import_does_not_duplicate(conn, frozen_batch):
    first = import_batch(conn, frozen_batch)
    second = import_batch(conn, frozen_batch)
    assert first["state_id"] == second["state_id"]
    assert second["receipt_status"] == first["receipt_status"]
```

- [x] 实现设计中 loading/failed/ready/partial/published/expired 六个 receipt 分支。每次状态修改同时维护 working 指针；不存在的/矛盾引用返回 inconsistent_import_state。
- [x] 分批参数绑定插入（每批1000评论），只从楼文件入库；用户副本仅校验。编码所有自由文本、保留扩展字段、来源metadata和异常行，标识/时间列不编码。入库后用解码对象重新核对字段与计数。
- [x] 连接丢失立即停止。用子进程在提交一批 loading 后退出，下一进程取到所有权锁后标 interrupted 并重导；测试不得仅通过手写 failed 绕过真实失锁路径。
- [x] 测试每个失败点 current 不变：字段拒绝、媒体编码、数据库写入错误、回读不一致；失败receipt有错误码且无正文。已成功但过期的批次不复活。
- [x] 运行 `python -m pytest backend/tests/storage/test_importer.py backend/tests/storage/test_import_recovery.py -q`，提交 `feat: import comment batches transactionally`。

## Task 4：发布、回收和查询分页

**Files:** publication.py、queries.py；backend/tests/storage/test_publication.py、test_queries.py。

- [x] 写 partial 不替换 current、无 current 返回 partial working、陈旧候选拒绝和同截止时间冲突测试。
- [x] 实现发布前视频行锁、资格校验，事务内清空旧receipt引用、切换指针、级联删除旧正文、候选current和receipt published。失败回滚不改变当前可读内容。

```python
def test_partial_import_keeps_current(conn, current_state, partial_batch):
    result = import_batch(conn, partial_batch, publish=True)
    assert result["published"] is False
    assert read_state(conn, current_state["video_id"])["state_id"] == current_state["state_id"]
```

- [x] query limit 为1–500，默认100。游标包含state_id、查询类型与root/UID、root_rank（楼）、时间空值标记/值和ID排序键；拒绝把另一个用户或楼的游标用到本查询。未知作者用 identity=None 单独查询。
- [x] 根第一条；剩余created_at ASC NULLS LAST，再ID长度与C排序；分别实现非空时间段和空时间段的keyset比较，不用带NULL的普通元组比较。返回原始解码评论。
- [x] 用两个数据库连接测试 REPEATABLE READ 读者在发布回收期间读完旧快照，新请求旧state返回state_expired；核对只剩current及最多一个working，旧receipt无正文且expired。
- [x] 运行 `python -m pytest backend/tests/storage/test_publication.py backend/tests/storage/test_queries.py -q`，提交 `feat: publish and query discussion states`。

## Task 5：数据库再导出与命令行交接

**Files:** exporter.py、cli.py；backend/tests/storage/test_roundtrip.py、test_cli.py；更新 README.md。

- [x] 从一次REPEATABLE READ只读事务中取得整批所需元数据、楼/评论和异常记录，解码后退出事务；生成新export_id及exported_at，保留原采集区间和覆盖，不提升partial。
- [x] 用已有 build_batch/validate_batch/publish_batch 生成文件，复用--output，不另造目录格式。数据库JSONB键顺序差异不算内容变化，数组顺序与字符串必须完全一致。

```python
def test_database_roundtrip(conn, frozen_batch, tmp_path):
    result = import_batch(conn, frozen_batch)
    path = export_state(conn, frozen_batch.manifest["video_id"], tmp_path,
                        state_id=result["state_id"])
    assert validate_batch(path)["counts"] == frozen_batch.manifest["counts"]
```

另外逐条比较评论，仅允许export_id改变；manifest只允许再导出标识/时间及派生路径变化。原始source_export_id留在数据库，不冒充新批次ID。

- [x] 提供下列CLI：DATABASE_URL只来自环境；输出安全JSON状态，不输出连接串或正文。import默认只导入，--publish明确请求发布；partial仍不提升。退出码0操作成功、2参数或协议错误、3忙/过期/无法发布、4数据库失败。

```text
python -m app.storage.cli import --input <批次目录或current.json> [--publish]
python -m app.storage.cli publish --video-id <视频ID> --state-id <状态ID>
python -m app.storage.cli export --video-id <视频ID> [--state-id <状态ID>] --output <目录>
python -m app.storage.cli query --state-id <状态ID> (--root-id <根ID> | --uid <UID> | --unknown-author) [--limit 100] [--cursor <游标>]
```

- [x] 运行所有离线/数据库测试及Ruff；更新说明中的环境变量、迁移、样例命令及清理边界；提交 `feat: expose PostgreSQL import and export commands`。

## Task 6：持久化与现有调试数据验收

- [x] 在专用开发数据库上运行初始迁移；重启db服务后已导入状态仍可读取。禁止用 docker compose down -v 验证持久化。
- [x] 按本阶段实施授权，冻结 D:/Data 中指定视频的current入口再导入；只报告真实coverage，partial可用但不得宣称完整。输入路径由命令指定，不写入生产默认值。
- [x] 查询至少一个完整楼、一个跨楼UID与一类上下文缺口（仅数据确实存在时）；与冻结文件逐字段对照。不存在的场景用虚构集成测试证明，不伪造真实样例。
- [x] 导入两次不增行；从数据库导出到独立 output 验证目录，不替换正在供下游使用的 D:/Data 当前批次；运行现有validate CLI。
- [x] 汇报测试、真实导入覆盖、重启持久化及未完成事项；不生成入库报告，不提交用户数据。该阶段完成后再讨论网页与后台任务，未满足整个功能分支验收不合并main。

## 完成门槛

全部协议映射、状态机和查询测试在真实PostgreSQL上通过；故障恢复不破坏current；回读/再导出无损且特殊字符测试通过；保留规则可核对；测试环境与用户数据库隔离。没有数据库运行证据时，本阶段不能标完成。


## 实施收敛与剩余边界

SQLAlchemy Core、Alembic 迁移、pg-text-v1 编码、输入冻结、幂等/恢复、状态发布与回收、按楼/UID 查询及数据库 CLI 已实现。mapping.py 将已映射字段与未知扩展分开保存，semantic.py 比较忽略再导出身份和派生路径后的同时间批次，避免误拒绝等价再导出。

本地开发库使用独立命名卷，测试库使用独立临时数据与每测试schema。Windows 默认测试端口受系统限制，通过本地 TEST_POSTGRES_PORT 配置替换；不修改协议或生产默认路径。

真实验收使用从采集检查点重新生成的独立合规批次：原下游批次README已被外部填入内容，校验器拒绝但保留源文件。数据库再导出写入 D:/Data/postgresql-validation，避免覆盖下游编辑。该目录仅为本机调用参数，代码不固定视频或输出路径。

文件发布增加Windows占用错误的有限重试，以及已校验未发布目录的入口恢复；任何失败不得覆盖已有目标或当前批次。覆盖字段在发布返回时先解码，保持查询与协议结果一致。

数据库子阶段验收不表示源评论已经采全。当前真实数据仍为partial，访问受限和上下文缺口照常保留；后续来源访问复核、网站页面/小时任务接入及目标服务器部署独立处理，未完成整个功能分支前不合并main。
