# 增量刷新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 使用复选框记录实际进度；沿用 feat/discussion-cache，不在 main 实施。

**Goal:** 分阶段实现正文复用、选择性采集及共享小时缓存；首个执行单元仅改造存储正文复用。

**Architecture:** PostgreSQL 将状态成员与正文载荷分离，继续通过统一映射重建协议记录。采集器随后维护独立的核验证据，后台 worker 最后将冻结基线、任务所有权和原子物化串联起来。

**Tech Stack:** Python 3.12、PostgreSQL 18、SQLAlchemy Core、psycopg、Alembic、pytest、Ruff；沿用现有依赖。

**Spec:** [增量刷新设计](2026-09-06-incremental-refresh-design.md)、[后台缓存设计](2026-09-06-background-cache-design.md)、[存储基线](2026-09-06-postgresql-storage-design.md)、[协议2.0.0](../references/comment-export-protocol-v2.md)。

## 全局约束

- 当前状态：单元A已实现并通过隔离数据库验收；单元B的独立CLI已完成，见[采集核心计划](2026-09-06-incremental-collection-plan.md)；单元C1数据交接已完成，见[后台交接计划](2026-09-06-background-handoff-plan.md)；C2调度已完成，见[调度计划](2026-09-06-background-scheduling-plan.md)；D已由[HTTP计划](2026-09-06-http-api-plan.md)完成；E基础采集页面已实现，见[页面计划](2026-09-06-capture-page-plan.md)；真实端到端联调已完成，源端不可得内容明确保留 partial。现有开发数据库已完成备份恢复和升级，后续状态见[本地联调计划](2026-09-06-local-capture-integration-plan.md)。
- 一次执行一个单元，验收后再进入下一个；本文件 Task 1–4 是单元 A 的详细执行步骤，其余单元是依赖及验收边界，不是一次全部开发的授权。
- 继续保留完整 current 与最近 working；partial 不自动替换完整 current，不新增历史浏览。
- 协议2.0.0、v1读取兼容、英文目录、字符串ID、UID关联及原始正文不变。文件导出仍生成完整新批次。
- 普通读缓存不触发来源请求；北京时间自然小时内同视频一个逻辑任务；24小时 full 策略仅在用户主动刷新时判断。
- full 与 incremental 均不将未再次观察到的旧评论自动删除；数据库载荷回收仅删除已无状态引用的内部数据。
- 常规测试只用虚构批次与独立 bilibili_talks_test，每例使用 bt_test_<uuid> schema；未配置 TEST_DATABASE_URL 的跳过不能算数据库验收通过。
- 不调用真实 Bilibili/模型，不改 D:/Data 原始样本，不把凭据、日志或验证报告提交到 Git。

## 单元顺序与交付门槛

| 单元 | 交付范围 | 依赖与验收门槛 |
| --- | --- | --- |
| A：正文存储复用 | 迁移、载荷摘要、导入复用、查询/导出还原、无引用回收 | 本文件 Task 1–4；证明相同正文无需重复 INSERT，CLI 回归与迁移无损 |
| B：采集增量核心 | 持久基线工作集、主列表全扫描、变化楼选择、实际观察集合、逐楼核验证据 | A 验收后细化独立执行计划；连续两次快刷不退化全抓，保留旧记录仍诚实 partial，重启恢复一致 |
| C：后台任务与小时缓存 | PostgreSQL 队列、单 worker、小时归并、访问闸门、缓存版本、worker 原子物化 | B 验收后细化后台核心计划；失权停止、取消与发布竞争、旧 partial 持续可读、预算不重置 |
| D：HTTP 接入 | 设计中的提交/查询/刷新/管理员接口及持久配额 | C 验收后细化接口计划；缓存命中零来源请求、并发唯一、过期分页拒绝、管理员权限隔离 |
| E：基础采集入口 | 复用 D 接口，链接输入、任务进度与保存摘要 | 用户已收缩范围；讨论浏览及AI页面后续设计，不包含页面美化或上线 |

## 单元 A 文件与接口

以下为新增接口约定，不表示已有实现。现有公开 import_batch、export_state、分页查询与 CLI 参数不变。

| 文件 | 改动与职责 |
| --- | --- |
| backend/app/storage/payloads.py（新增） | 逻辑载荷拆分、确定性摘要、批量查重/插入、同视频无引用回收 |
| backend/app/storage/records.py（新增） | 统一成员与载荷联接查询，供映射、导入复核、语义比较和导出使用 |
| backend/app/storage/schema.py | comment_payloads，comments.video_id/payload_id，组合外键及索引 |
| backend/app/storage/mapping.py | 分离成员值与载荷；record_from_row 继续接收重建的 content/extra_fields 列 |
| backend/app/storage/importer.py、publication.py | 导入采用载荷引用；保持现有事务、receipt、锁和发布语义；安全回收 |
| backend/app/storage/queries.py、exporter.py、semantic.py | 所有正文读取改走统一联接；排序、游标和比较语义不变 |
| backend/migrations/versions/0002_comment_payloads.py（新增） | 事务内建表、回填、无损校验、建立约束后移除成员表重复正文列；冻结迁移逻辑 |
| backend/tests/storage/test_payloads.py、test_payload_migration.py（新增） | 摘要/复用及带既有数据升级、降级验证 |
| backend/tests/storage/test_schema.py、test_mapping.py、test_importer.py、test_publication.py、test_queries.py、test_roundtrip.py、conftest.py | 更新直接写表夹具，覆盖导入/查询/回收兼容性 |

```python
# payloads.py：输入均为已解码的合法协议对象；不执行来源请求。
def payload_document(record: dict) -> dict: ...
def payload_digest(document: dict) -> str: ...
def resolve_payloads(conn, video_id: str, records: list[dict]) -> dict[str, str]: ...
def prune_payloads(conn, video_id: str) -> int: ...

# records.py：返回 SQLAlchemy Select，保留成员列名及 content/extra_fields。
def comment_select(): ...
```

resolve_payloads 返回 comment_id 到 payload_id 的映射，不自行提交事务。prune_payloads 只回收指定视频且不存在任何 comments 引用的载荷，由已持视频锁的调用方执行。

## Task 1：明确载荷边界与摘要

**Files:** 新增 payloads.py、test_payloads.py；调整 mapping.py、test_mapping.py。

- [x] 在 test_payloads.py 添加以下纯函数回归，先运行并确认因接口尚未实现而失败：

```python
from app.storage.payloads import payload_digest


def test_payload_hash_ignores_object_key_order_not_array_order():
    left = {"content": {"text": "讨论", "media": [1, 2]}, "extra_fields": {}}
    reordered = {"extra_fields": {}, "content": {"media": [1, 2], "text": "讨论"}}
    changed = {"content": {"text": "讨论", "media": [2, 1]}, "extra_fields": {}}
    assert payload_digest(left) == payload_digest(reordered)
    assert payload_digest(left) != payload_digest(changed)
```

- [x] 实现 payload_document：content 原样保留；extra_fields 保留当前映射中的 top/author 未知扩展。已知昵称、点赞、关系、UID、时间和批次身份仍在成员字段或状态元数据中，不进入载荷。未知扩展暂不猜测其是否属于可变统计，修改它会生成新载荷，不能静默丢失。
- [x] 实现规范摘要，解码后计算，入库时再使用 pg-text-v1；禁止对已编码 JSON 再编码。规范实现：

```python
import hashlib
import json
from decimal import Decimal


def normalize_numbers(value):
    if isinstance(value, float) and value.is_integer():
        return int(Decimal(str(value)))
    if isinstance(value, list):
        return [normalize_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_numbers(item) for key, item in value.items()}
    return value


def payload_digest(document: dict) -> str:
    canonical = json.dumps(normalize_numbers(document), sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
```

- [x] 增加协议样例测试：只改 nickname/like_count/export_id/collected_at，payload_document 相等；正文、图片、表情或未知扩展变化则不等；NUL、反斜杠和孤立代理字符经映射无损恢复。运行 `python -m pytest backend/tests/storage/test_payloads.py backend/tests/storage/test_mapping.py -q`。
- [x] 测试通过后提交 `feat: define reusable comment payloads`。

## Task 2：迁移与正文引用约束

**Files:** schema.py、0002_comment_payloads.py、test_payload_migration.py、test_schema.py、conftest.py。

- [x] 增加隔离旧版数据库迁移用例：在测试 schema 升至0001，插入 current 与 working，其中同ID相同正文、同ID不同正文各一组；保存完整解码记录，升级后比较每条记录和状态计数。迁移测试须使用冻结的旧版DDL/插入SQL，不能用已修改的 schema.py 构造旧表。
- [x] 建立 comment_payloads：payload_id UUID主键，video_id 外键，comment_id TEXT，content_hash 64位十六进制，content/extra_fields JSONB；UNIQUE(video_id,comment_id,content_hash)，另建 UNIQUE(video_id,comment_id,payload_id) 供成员外键引用。
- [x] comments 添加 video_id/payload_id；分别以 (video_id,state_id) 指向 discussion_states 和 (video_id,comment_id,payload_id) 指向 comment_payloads，阻止跨视频或跨评论误挂。保留根楼外键、排序生成列与原索引；新增载荷引用索引支持回收检查。
- [x] 在迁移事务内按稳定成员主键分块回填；从 states 取得 video_id，解码旧正文后用 Task 1 相同的冻结算法生成摘要并编码。迁移自带版本固定的编码/摘要逻辑，不导入可随业务变化的 payloads.py/schema.py。
- [x] 验证每条旧 content/extra_fields 与联接载荷相等、无空引用、无身份错配后，设 NOT NULL 并移除 comments 中重复正文列。任一不一致使整次迁移回滚；downgrade 先按引用恢复旧列并校验，再移除引用和载荷表。
- [x] 测试跨身份引用被数据库拒绝、同ID不同正文分别保存、同正文重复只一份、升级/降级/再升级保持特殊字符及扩展。运行 `python -m pytest backend/tests/storage/test_payload_migration.py backend/tests/storage/test_schema.py -q`。
- [x] 与 Task 3 集成通过后提交迁移及兼容代码，避免留下不能正常导入的中间提交。真实用户数据库不在本任务直接升级；应用切换需停写窗口、备份与核验，不宣称支持新旧代码混跑。

## Task 3：导入复用与统一查询

**Files:** payloads.py、records.py、mapping.py、importer.py、queries.py、exporter.py、semantic.py；相关存储测试。

- [x] 扩展真实 PostgreSQL 测试：两个合法状态共享相同评论载荷；只改点赞/昵称不新建载荷；改正文新增载荷；重复 receipt 不增加成员和载荷。测试使用 frozen_case 及现有冻结批次工具，保留时间/新鲜度约束。
- [x] resolve_payloads 先批量读取已有身份与摘要，再比较解码内容，命中仅返回引用；缺失项才 INSERT，保留冲突后的重读校验。摘要相同而内容不同返回 payload_hash_conflict，不静默引用。单次批次重复ID不同内容仍由现有协议校验拒绝。
- [x] 将 comment_values 的成员字段和载荷拆分；成员 INSERT 与该批载荷写入使用同一事务。保留 importer 当前分批提交方式，worker 原子导入属于单元 C，不能混入本任务。
- [x] 实现 comment_select 联接完整身份键并返回与 record_from_row 兼容的映射列。替换 importer 最终复核、queries、exporter、semantic 的全部直接正文读取；使用 `rg -n 'select\(comments|comments\.c\.(content|extra_fields)' backend/app/storage` 检查遗漏。
- [x] 在测试中记录 SQL 执行：第二个合法状态全部命中时，没有对 comment_payloads 的 INSERT；不能仅检查最终行数，因为 ON CONFLICT 仍可能发送重复大正文。
- [x] 运行 `python -m pytest backend/tests/storage -q`，核对根评论优先、UID分类、空时间、大ID、扩展字段、pg-text-v1、v1/v2导出与进程中断恢复。测试必须实际连接独立 PostgreSQL。
- [x] Task 2 与 Task 3 全部通过后提交 `feat: reuse payloads across discussion states`。

## Task 4：载荷回收与单元 A 验收

**Files:** publication.py、payloads.py、test_publication.py、test_importer.py、test_roundtrip.py、README.md；更新本计划状态。

- [x] 新增回收测试：删除 working 不删除 current 共用载荷；发布替换后只删除无引用载荷；导入失败留下的载荷可以在持锁清理时回收；另视频载荷不受影响；重复读快照仍可完成旧查询。
- [x] 在同视频锁及现有状态清理事务中执行回收；先完成新成员引用，再清理无引用载荷。对失败清理与下一次持锁导入覆盖相同规则，不增设全库后台GC或修改用户导出目录。
- [x] 使用测试批次完成 v1/v2输入 → 数据库 → v2导出 → 现有 validate_batch；比较全部正文、身份、关系、时间及未知扩展，仅允许约定的批次身份和派生路径变化。
- [x] 运行 `python -m pytest backend/tests -q` 和 `python -m ruff check backend scripts`，确认 PostgreSQL 用例没有因缺配置全部跳过；用隔离测试 schema 验证真实0001数据升级，不能用空表建库代替迁移验收。
- [x] README 说明存储已复用正文，但来源仍按原CLI行为采集；更新本计划 A 的实际结果，证据只在会话或忽略目录保存。提交 `feat: reclaim unreferenced comment payloads`。
- [x] 单元 A 验收后停止，报告变化、测试、迁移限制；再制定单元 B 详细执行计划，不自动实施队列、HTTP或页面。整个 feat/discussion-cache 完成前不合并 main；最终合并后按 AGENTS.md 自动推送。

## 后续单元必须承接的设计约束

B 的计划必须覆盖：complete/needs_check/source_unavailable 与本轮处理方式分离；主楼真实到尾；新楼、变化楼和实际未完成范围补核；同计数换ID不误报无变化；full计划与完成证据；本轮观察集合和继承集合分开；内部证据随SQLite事务持久化；CLI默认兼容；不增加v2协议字段。

C 的计划必须覆盖：首次领取冻结模式/基线；一致快照冻结完整正文和内部证据；单调缓存版本及所有导入/发布入口维护；base_state_id不阻碍回收；上下文与任务/视频/批次摘要绑定；失权回调、预算/小时不重置、全局来源闸门；物化事务中核对版本/取消/所有权并提交证据与结果；无可信证据的文件导入采用保守初值。

D 的计划必须覆盖：全部后台设计接口、HTTP错误语义、缓存读取零来源调用、输入域名/长度限制、持久配额、管理员令牌隔离、未知作者入口、过期state游标处理。默认仍优先完整current，最新partial独立入口；未取得用户明确选择前不改变显示规则。


## 单元 A 交付状态

- Task 1：完成，提交 f73e958；Task 2–4：完成，组合提交 3e24f41，保持迁移、全部读写入口和回收规则同步切换。
- 237项后端测试通过，包含真实PostgreSQL隔离schema的有数据升级/降级/再升级、失败事务回滚；Ruff通过。两条现有FastAPI/Starlette依赖弃用提示仍存在。
- 子Agent完成迁移实现与代码审阅；数值表示导致伪摘要冲突的问题已复现并修正，运行时与冻结迁移规则保持一致。
- 本单元未升级现有开发数据库、未重抓来源、未修改D:/Data样本。实际升级按README停写、备份和核验步骤单独执行。
- 单元B已随后完成独立CLI实现及验收，见[采集核心计划](2026-09-06-incremental-collection-plan.md)；网站小时后台任务仍未实现。
