# 通用视频评论采集与导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 当前会话内逐项执行，不默认派发子 Agent。

**Goal:** 独立命令行脚本接收 Bilibili 视频链接，采集可取得的主评论与回复，输出符合固定协议的按楼和按 UID 文件。

**Architecture:** 来源适配器负责链接与分页，采集器负责限速、恢复和覆盖判定，导出器只消费冻结工作集。文件发布和消费者读取独立于网络采集，后续网站可复用这些模块。

**Tech Stack:** Python 3.12、httpx、jsonschema、pytest、标准库 sqlite3（仅命令行恢复工作集）、现有 Ruff 与依赖锁定工具。

**Spec:** [评论导出协议 1.0.0](../references/comment-export-protocol-v1.md)、[导出设计](2026-09-05-comment-export-design.md)。字段契约以协议为准，计划不得覆盖协议。

## 全局约束与验收边界

- 当前分支 `feat/discussion-cache`。Task 1–6 的代码已实现并通过当前离线测试；真实样例的逐楼采集验收仍在进行，不能视为全量完成。
- URL 是运行参数。`https://www.bilibili.com/bangumi/play/ep4292435/` 只用于人工调试，禁止出现在生产默认值、分支判断或固定视频身份中。
- 首版接受 HTTPS 的 `www.bilibili.com/video/BV...` 和 `www.bilibili.com/bangumi/play/ep...`，可带分享查询参数；未知格式明确拒绝。短链、作品自动发现及其他评论类型不在本次支持范围。
- 所有数字身份保持字符串；正文不清洗，UID 不由昵称推断；两种投影来自同一冻结批次。
- 凭据通过 `BILIBILI_COOKIE_FILE` 指向受保护文件，仅发送至固定 api.bilibili.com；不打印、不写 Git、不随重定向转发。不从桌面浏览器读取凭据。
- 本轮真实导出使用用户指定的 `D:/Data` 作为 --output，视频容器为其下 bilibili-video-<aid>；该路径仅是本机配置，不硬编码。协议中的 data/exports 是默认输出根，替换根不改变内部格式。恢复工作数据仍在忽略的 data/ 内，测试使用虚构数据且不联网；不提交报告、原始响应或采集内容。
- PostgreSQL 仍是网站数据库方案。本次 sqlite3 仅供单机 CLI 中断恢复，不能作为网站共享缓存交付。
- 不实现 HTTP 输入页面、共享小时刷新调度、模型调用和正式部署。hour_bucket 只记录本次任务创建的北京时间自然小时。
- 按任务验收；全部完成且符合所在功能分支完整范围后，才按 AGENTS.md 合并与推送 main，不能因本子任务结束提前合并未完成的网站功能。

## 模块与接口约定

新包 `backend/app/comment_export/`，包含 `__init__.py`。内部 JSON 对象使用 `dict[str, Any]`，由 Schema 与语义校验器保证契约，不引入与协议并行的另一套字段名称。

| 文件 | 责任与公开接口 |
| --- | --- |
| contract.py | `validate_record(kind: str, value: dict) -> None`；`ContractError(ValueError)` |
| schemas/*.json | common、comment、manifest、thread、user、current、unclassified 七个 Schema，Draft 2020-12，本地引用 |
| export.py | `build_batch(records: list[dict], manifest: dict, destination: Path) -> Path`：输入已归一化评论及批次元数据，生成索引、计数和投影；目标须不存在 |
| validation.py | `validate_batch(batch_dir: Path) -> dict`：全文件/跨文件核对，通过返回 manifest，否则 ContractError |
| publication.py | `publish_batch(batch_dir: Path, container: Path) -> Path`；`read_current(container: Path) -> tuple[dict, list[dict]]`：返回已完整校验的 manifest 与去重评论，按协议有界重读 |
| source.py | `resolve_video(url: str, client: httpx.Client) -> dict`：返回 source、canonical_url、title；分页方法返回结构化响应，不直接落盘 |
| signing.py | `sign_main(params: dict[str, str], img_key: str, sub_key: str, timestamp: int) -> dict[str, str]`：纯 WBI 签名函数 |
| checkpoint.py | `Checkpoint(path: Path)`：`commit_page(key: str, comments: list[dict], progress: dict) -> None`；`freeze() -> tuple[list[dict], dict]` |
| collector.py | `collect(url: str, work_dir: Path, client: httpx.Client, max_requests: int, resume: bool) -> tuple[list[dict], dict]`：返回冻结评论和导出元数据 |
| cli.py | `main(argv: list[str] | None = None) -> int`：collect、export、validate 三个子命令 |

源响应异常采用 `CollectionStopped(RuntimeError)`，携带安全原因码，不含请求头/凭据。解析/网络失败保留检查点，不用异常文本冒充协议原因码。所有实际内部辅助类型在所在模块定义，不依赖临时探查脚本导入。

## Task 1：固定协议的结构校验

**Files:** 新增 contract.py、schemas/、`backend/tests/test_export_contract.py`、`backend/tests/fixtures/comment_export/valid/`；修改 backend/pyproject.toml、scripts/lock_dependencies.py、两个 requirements 锁文件。将 httpx 提升为运行依赖，新增 jsonschema 运行依赖；setuptools 包数据包含 schemas/*.json。

**Interfaces:** 产出 validate_record、ContractError，kind 为 comment、manifest、thread、user、current、unclassified。

- [ ] 写虚构合法记录以及拒绝数字型 UID 的测试，先运行并确认因接口缺失而失败。

```python
def test_rejects_numeric_uid(valid_comment):
    valid_comment["author"]["uid"] = 123
    with pytest.raises(ContractError):
        validate_record("comment", valid_comment)
```

- [ ] 将协议字段、null、枚举、日期与字符串身份逐项映射到 Schema；启用日期格式检查，拒绝重复 JSON 键与非有限数值。Schema 不得联网解析引用；同主版本额外字段可保留。
- [ ] 扩展测试覆盖必填缺失、未知 major、有效超大 ID、UUID、媒体项、未知作者和父关系组合；以 protocol 的虚构示例构造 valid_comment fixture。
- [ ] 安装开发依赖后运行 `python scripts/lock_dependencies.py`；执行 `python -m pytest backend/tests/test_export_contract.py -q`，验证 Schema 随包安装可读。
- [ ] 检查 diff 后提交 `feat: validate comment export contract`。

## Task 2：冻结数据的双重归类导出

**Files:** 新增 export.py、validation.py、`backend/tests/test_comment_export.py`，扩展虚构 fixtures。

**Interfaces:** 消费 Task 1 校验器；提供 build_batch、validate_batch。manifest 输入包含采集身份/时间/覆盖，计数与索引由导出器重算，不信任调用方数量。

- [ ] 构造两楼、同 UID 跨楼、同名不同 UID、未知 UID 和缺父记录的 fixture。测试根评论优先排序、用户跨楼集合及对象一致性，先运行看到缺失实现失败。

```python
def test_projection_counts(frozen_case, tmp_path):
    records, metadata = frozen_case
    batch = build_batch(records, metadata, tmp_path / "batch")
    manifest = validate_batch(batch)
    assert manifest["counts"]["comments"] == len(records)
    assert (batch / "README.md").read_bytes() == b""
```

- [ ] 实现字符串 ID 数值排序、昵称标签清理与长度限制、_unknown 分类，输出 JSON/JSONL 和协议元数据；每条记录使用同一对象值写两份。
- [ ] 实现全量语义校验：身份集合、计数、重复 ID、路径逃逸、批次一致、两投影深度相等、用户覆盖字段继承。unclassified 不进入正常投影，保留异常文件并强制 partial。
- [ ] 追加篡改用户文件正文、跨楼归属错误、正文换行/媒体、空评论区和缺根楼测试。零评论区允许空索引但覆盖必须有采集证据。
- [ ] 运行 `python -m pytest backend/tests/test_comment_export.py -q`，通过后提交 `feat: export thread and user comment files`。

## Task 3：发布入口与读取一致性

**Files:** 新增 publication.py、`backend/tests/test_export_publication.py`。

**Interfaces:** 消费 validate_batch；提供 publish_batch、read_current。读取错误使用 ContractError，消息为协议错误码；发布失败保留旧入口。

- [ ] 写指针与 manifest 不符、旧目录中途消失的测试，用 monkeypatch 在读取文件的边界切换入口，不使用定时 sleep 制造竞争。

```python
def test_published_batch_is_readable(valid_batch, tmp_path):
    container = tmp_path / "video"
    publish_batch(valid_batch, container)
    manifest, comments = read_current(container)
    assert len(comments) == manifest["counts"]["comments"]
```

- [ ] 用同目录临时指针和 `os.replace` 发布；每视频持有进程文件锁（Windows msvcrt、Linux fcntl），第二发布者立即返回 busy，不删除仍被引用的数据。
- [ ] 按协议实现最多两次重新开始、整批丢弃、入口未变时 invalid_export 等规则；read_current 返回前完整校验，不产生外部副作用。
- [ ] 测试替换失败、读取重试耗尽、无入口、符号链接逃逸、发布 partial 不替换既有 verified。恢复清理只处理该视频容器内部未引用批次。
- [ ] 运行 `python -m pytest backend/tests/test_export_publication.py -q`，提交 `feat: publish consistent export batches`。

## Task 4：通用链接与 Bilibili 适配器

**Files:** 新增 source.py、signing.py、`backend/tests/test_bilibili_source.py`。测试仅使用 httpx.MockTransport。

**Interfaces:** 提供 resolve_video 和 sign_main；source.py 同时定义 `fetch_main(source, cursor, client, signing_keys) -> dict`、`fetch_replies(source, root_id, page, client) -> dict`。

- [ ] 写至少两个不同虚构 ep_id、一个 BV 和分享参数别名的解析测试，以及非 HTTPS、用户信息、端口、内网/伪装域名与不支持路径拒绝测试。

```python
def test_rejects_untrusted_url(client):
    with pytest.raises(ValueError):
        resolve_video("https://www.bilibili.com.evil.example/video/BVfake", client)
```

- [ ] 使用固定端点 `/pgc/view/web/season?ep_id=...` 定位对应 episode，再通过 `/x/web-interface/view` 核对 aid/BV；仅来源响应决定身份。URL 字符串不能直接作为任意网络请求目标。
- [ ] 主楼使用 `/x/v2/reply/wbi/main` 时间排序 mode=2、原样不透明 cursor；楼中楼使用 `/x/v2/reply/reply` type=1、ps=20，检查页码/根 ID。只把预览回复作为预览，不能据其判楼完成。
- [ ] WBI key 从固定 API 的 `/x/web-interface/nav` 返回公开 wbi_img 字段取得，失败则明确停止；不硬编码探查时 key，不加载下载的 vendor JS 为运行依赖。签名排序、字符过滤和混排逻辑通过固定输入测试，真实可用性在 Task 6 验证。
- [ ] 客户端显式应用 UA、20 秒超时、TLS 校验、follow_redirects=False；登录文件不进入测试 fixture，模拟拒绝/重定向并验证停止。
- [ ] 运行 `python -m pytest backend/tests/test_bilibili_source.py -q`，提交 `feat: resolve video links and fetch reply pages`。

## Task 5：持久恢复与覆盖判定

**Files:** 新增 checkpoint.py、collector.py、`backend/tests/test_comment_collection.py`。

**Interfaces:** 消费 Task 4；产出冻结的 Task 2 输入。工作集以 canonical aid 隔离，恢复时核对 URL 解析身份；新采集与旧任务不得混用。

- [ ] 创建 SQLite task、pages、comments、threads、unclassified 表。记录模式版本、任务 UUID、创建时间/hour_bucket、源身份、请求计数、状态与下一游标；comments 按 video_id/comment_id 唯一。
- [ ] 先写事务故障测试，断言页内容和下一游标同时回滚；请求预算在请求前持久扣减，不能通过崩溃重启重置。

```python
def test_empty_checkpoint_is_not_complete(tmp_path):
    checkpoint = Checkpoint(tmp_path / "work.sqlite3")
    records, metadata = checkpoint.freeze()
    assert records == []
    assert metadata["coverage"]["status"] == "partial"
```

- [ ] 串行请求，至少 2 秒间隔；默认总预算 12000，单楼最多 1000 页。HTTP/业务拒绝和网络异常停止并保存进度；不自动更换客户端或绕过限制。普通暂停可 --resume，受限恢复必须通过单次请求复核后另行明确授权，不把 --resume 当自动解禁。
- [ ] 主楼以 cursor.is_end 及有效结构结束；检测游标循环，连续五页只有旧 ID 时停止核对。回复以有效页元数据、唯一数和尾页判定；零回复楼也请求核验一次，若发现新增回复则继续分页。
- [ ] 数量变化的楼从头有界复核一次；对照 ID、上下文内容与尾页，仍变化保持 partial。缺父、未知作者等独立记录 gaps，避免把分页完成等同上下文完整。
- [ ] 测试跨页重复、空页异常、全旧但游标前进、零回复变有回复、受限、预算耗尽、身份冲突、中断恢复、不同视频隔离。冻结后不得边采集边导出。
- [ ] 运行 `python -m pytest backend/tests/test_comment_collection.py -q`，提交 `feat: checkpoint comment collection and coverage`。

## Task 6：CLI 集成与调试视频验收

**Files:** 新增 cli.py、`backend/tests/test_comment_export_cli.py`；修改 README.md，提供本地和 Linux 命令、凭据配置及停止状态说明，不写实测报告。

**Interfaces:** CLI 退出码：0 为操作成功（collect 仅 verified），2 为参数/协议错误，3 为 partial 或采集停止，4 为发布/读取失败。错误输出不含评论正文和凭据。

- [ ] 用 MockTransport/注入采集函数测试 main：未传 URL 不使用默认样例，两个不同 URL 分别解析，partial 返回 3，validate 遇损坏返回 2。
- [ ] 实现以下 CLI，--work-dir 默认 data/collection，--output 默认 data/exports；export 从已冻结工作集生成，不发起网络请求；validate 接收批次目录。

```text
python -m app.comment_export.cli collect --url <视频链接> [--resume] [--max-requests 12000]
python -m app.comment_export.cli export --work-dir <该任务工作目录>
python -m app.comment_export.cli validate --batch <批次目录>
```

- [ ] 执行 `python -m pytest backend/tests -q` 和 `python -m ruff check backend scripts`。检查 `git check-ignore data/exports/example data/collection/example`；确认凭据、数据及日志均未被暂存。
- [ ] 离线检查通过后，使用用户已配置的 BILIBILI_COOKIE_FILE，先做新样例小预算运行，核对解析身份，再按同一任务 --resume 完成可访问范围。以下仅为调试调用，不是默认参数：

```text
python -m app.comment_export.cli collect --url https://www.bilibili.com/bangumi/play/ep4292435/ --max-requests 12000
```

小预算检查通过适配器单次调用完成，不改变持续任务的总预算或自动解除受限状态。新拒绝、无法取得 key 或持续变化时保留 partial 并报告；不得为通过验收修改完整性口径。

- [ ] 导出并运行 validate，核对根楼/回复/用户数量、两投影一致、空 README、缺口状态；只在对话汇报实际结果，不提交真实数据。
- [ ] 提交 `feat: expose comment collection export commands`。列明未验证的目标服务器环境；若真实端点不可用，明确真实验收未完成，不勾选通过。

## 计划验收与后续边界

Task 1–3 覆盖协议结构、全部导出文件和发布读取；Task 4–5 覆盖可变 URL、来源身份、分页、恢复与完整性；Task 6 覆盖脚本入口及真实调试。图片仅保存描述与 URL；网站 PostgreSQL 接入、页面、小时刷新控制和模型分析不在此计划。

每项通过相关测试后再进入下一项，失败先修复或明确阻塞。计划本身不构成一次性执行所有后续阶段的授权；执行时优先在当前会话按步骤推进，无需为普通代码检查重复询问。


## 实施状态与交接（2026-09-06）

- 协议校验、双重归类、发布读取、通用链接适配、事务检查点及 CLI 已实现。共享虚构 fixture 位于 backend/tests/conftest.py，归一化独立在 normalization.py；不复制真实数据到测试目录。
- 用户指定本轮输出根 D:/Data，默认输出根仍为 data/exports；标准内部目录结构不变。恢复工作集仍位于仓库忽略的 data/collection。
- 新视频主楼采集及部分结果导出已打通，完整楼中楼覆盖验收未完成。运行进度、具体数量及临时证据只在对话或忽略目录，不写入仓库报告。
- 下游可读取明确 partial 的发布批次开展接口开发。最终完整性以实际 manifest 为准；目标服务器运行、共享数据库和网页仍属于后续任务。
