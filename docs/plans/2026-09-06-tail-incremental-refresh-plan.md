# 楼中楼尾部增量 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkboxes for tracking. 实现与验收已完成。

**Goal:** 在回复数增加的大楼上，以可信分页基线和重叠校验减少请求，同时保留整楼回退与partial语义。

**Architecture:** 纯函数负责尾部证据和候选校验；SQLite分离保存候选页，验证通过后原子合并。采集器选择尾部/完整路径，数据库交接保留可选版本化证据；公开协议不变。

**Tech Stack:** Python 3.12、HTTPX、SQLite、SQLAlchemy Core、PostgreSQL、pytest、Ruff；不增加依赖。

**Spec:** [已批准设计](2026-09-06-tail-incremental-refresh-design.md)。用户已批准设计，用户已确认执行。

## Global Constraints

- 分支 `opt/comment-collection-performance`，基于main；本功能设计、计划和代码保持同分支。
- 每页20条、旧回复至少4页、有效模式incremental、回复数严格增加；首次/显式full/到期完整核对不走尾部路径。
- 保留主评论完整游标扫描、共享限速、请求预算、取消/租约、小时缓存及显式刷新规则。
- 不推进空楼跳过、并发、直接入库、检查点整体性能重构或分析页面。
- 不能从ID或时间排序重建来源顺序；旧证据缺失时对需要更新的楼完整获取。
- 尾部成功仍为partial，不更新完整核对时间，不新增公开导出枚举；v2输出及v1读取保持兼容。
- 常规测试不联系Bilibili。真实验证结果、样本和探测脚本仅存忽略目录。
- 数据库使用现有refresh_context JSON，无新增PostgreSQL列或Alembic迁移；SQLite增加候选表，旧工作集打开时按现有方式幂等创建。
- 部署前停止旧worker，保持工作集及数据库备份；不与不识别新恢复状态的旧代码混跑。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| 新增 `backend/app/comment_export/tail.py` | 证据、选择条件、页范围和纯校验 |
| 新增 `backend/app/comment_export/tail_collection.py` | 受预算保护的尾页请求、候选保存及回退结果 |
| 修改 `checkpoint.py` | 候选事务、尝试编号、原子合并 |
| 修改 `collector.py`、`incremental.py` | 完整获取建立证据、选择策略、完成与覆盖语义 |
| 修改 `backend/app/storage/handoff.py`、`baseline.py` | 可选证据交接校验和恢复 |
| 新增 `backend/tests/test_tail_evidence.py`、`test_tail_checkpoint.py`、`test_tail_collection.py` | 纯算法、事务、请求数与异常测试 |
| 新增 `backend/tests/storage/test_tail_handoff.py` | 存储往返、发布冲突和旧数据兼容 |
| 修改 `README.md`及当前计划 | 使用方式、限制和实际完成状态 |

下文未写目录的采集文件均位于 `backend/app/comment_export/`。Python命令在已激活虚拟环境的仓库根目录运行。

## Task 1：纯算法及可信边界

**Files:** `tail.py`、`backend/tests/test_tail_evidence.py`。

**Interfaces:** 使用规范化评论dict；`old_rows`为该视频的评论ID到记录映射，保留全视频身份冲突检测能力。定义：

```python
@dataclass(frozen=True)
class TailPlan:
    root_id: str
    old_count: int
    new_count: int
    page_size: int
    start_page: int
    last_page: int
    anchor_ids: tuple[str, ...]

class TailMismatch(ValueError):
    pass

def tail_start_page(old_count: int, page_size: int = 20) -> int:
    return max(1, (old_count + page_size - 1) // page_size - 1)

def build_tail_evidence(ordered_replies: list[dict], root_row: dict,
                        source_count: int, checked_at: str) -> dict | None: ...
def validate_tail_evidence(value: object, root_row: dict,
                           old_rows: dict[str, dict], *, snapshot_at: str) -> dict | None: ...
def select_tail_plan(state: dict, root_row: dict, old_rows: dict[str, dict],
                     mode: str, new_count: int) -> TailPlan | None: ...
def validate_tail_pages(plan: TailPlan, pages: list[dict],
                        old_rows: dict[str, dict], root_row: dict,
                        checked_at: str, prior_evidence: dict) -> tuple[list[dict], dict]: ...
```

签名中的省略号仅表示接口声明，函数行为固定如下：build只接受完整、有序、去重、数量一致且时间非递减的回复，保存最后两页ID；证据包含version=1、source_count、page_size、anchor_start_page、anchor_ids、root_signature、root_identity（comment_id/root_id/author_uid）、latest_tail_checked_at、last_full_checked_at。validate检查类型、ID唯一/归属、锚点长度与偏移、时间有时区且不超出snapshot_at所指定的对应快照、正文指纹；无效或未知版本返回None，不作为加速依据。

select只消费经过validate的证据，并额外检查4页阈值、严格增加、本轮确实观察到主楼、无unavailable/未知身份/沿用额外回复造成的数量矛盾、根身份正文一致及有效模式。返回计划或None。validate_tail_pages按页码顺序验证请求范围、每页固定总数/大小、记录归属、时间顺序、旧锚点完全匹配、锚点后全部为真正新增ID、每页实际长度和新增总量；失败抛带安全原因码的TailMismatch。成功返回可合并规范化记录及新证据，保持历史完整核对时间。

- [x] 写以下失败测试及完整断言，再运行 `python -m pytest backend/tests/test_tail_evidence.py -q` 确认失败来自缺失行为。

```python
@pytest.mark.parametrize('old_count,expected', [(61,3),(397,19),(400,19)])
def test_overlap_start(old_count, expected):
    assert tail_start_page(old_count) == expected
```

同文件以确定的规范化记录构造source_count=397基线：ID为字符串、同root/UID、created_at递增，source顺序由列表给出。断言398条时计划19..20，401对400时19..21；未知证据版本、重复锚点、跨楼ID、未来时间、旧60条、小楼、full、数量不增、root正文改变均拒绝快速路径。另测相同发布时间允许，ID值不递增但来源顺序稳定仍可接受。

- [x] 实现上述纯函数；尾部通过后锚点取新最后两页，保留last_full_checked_at。不能将大整数ID转浮点。
- [x] 增加连续两次追加、重叠正文更新、来源附加字段变化和中间等量替换边界测试；未读变化必须通过partial语义表达，不能声称已检查。
- [x] 跑本文件测试及 `python -m ruff check backend/app/comment_export/tail.py backend/tests/test_tail_evidence.py`，通过后提交 `feat: define guarded tail refresh evidence`。

## Task 2：候选页与原子检查点

**Files:** `checkpoint.py`、`backend/tests/test_tail_checkpoint.py`。

**Interfaces:** 在Checkpoint新增 `begin_tail(root_id, plan: dict, progress: dict) -> int`、`stage_tail_page(root_id, attempt: int, page: int, payload: dict, progress: dict) -> None`、`read_tail_pages(root_id, attempt) -> list[dict]`、`promote_tail(root_id, attempt, rows: list[dict], progress: dict) -> None`、`abandon_tail(root_id, attempt, progress: dict) -> None`。plan持久化TailPlan的JSON形式；payload包含响应page的num/size/count与规范化root/replies。

- [x] 先添加事务测试：stage后freeze看不到候选评论；关闭重开后候选页仍可读；第二条候选发生身份冲突时promote整体回滚，原评论、metadata和progress不变。
- [x] 幂等建表：`tail_attempts(root_id TEXT PRIMARY KEY, attempt INTEGER, status TEXT, plan_json TEXT)`；`tail_pages(root_id TEXT, attempt INTEGER, page INTEGER, payload TEXT, PRIMARY KEY(root_id,attempt,page))`。status仅active/promoted/abandoned；同楼新尝试编号递增。
- [x] begin创建新尝试并保存小型控制状态；stage只写候选表及进度，不将未验证的观察ID/来源计数加入正式metadata。相同尝试同页的相同payload可幂等，不同payload不得静默覆盖。
- [x] promote在单个SQLite事务中核对active尝试、执行既有身份校验、更新评论、metadata、progress、checkpoint_revision及promoted标记；不能调用会提前提交的嵌套事务方法。保留 `_control_progress` 的最大已用预算/最小总预算规则。
- [x] abandon只将候选尝试与回退状态持久化，不修改已确认评论，不减少累计请求。添加重复promote、旧attempt写入、回退页键隔离测试。
- [x] 运行 `python -m pytest backend/tests/test_tail_checkpoint.py backend/tests/test_checkpoint.py -q` 和Ruff；通过后提交 `feat: stage tail pages before atomic promotion`。

## Task 3：完整获取生成证据与策略选择

**Files:** `collector.py`、`incremental.py`、`backend/tests/test_tail_collection.py`、`backend/tests/test_incremental_collection.py`。

**Consumes:** Task1的build/validate/select；Task2不在本任务触发来源尾部请求。

- [x] 扩展隔离假来源，支持任意页：构造root_id='100'、reply_id=str(1000+i)，count=n，page.replies使用 `reply_ids[(pn-1)*20:pn*20]`。调用列表记录每个root/pn，不依赖真实接口。
- [x] 写完整获取397条后生成两页锚点、旧无序工作集不伪造证据、完整重读因变化进入第二轮后只用最终一轮顺序的失败测试。
- [x] 在完整获取的最终通过轮次按响应顺序积累ID，不使用现有seen的排序结果；调用build前确认全部来源回复已取到且没有额外沿用记录。仅保存必要的尾部证据，不将额外整个来源正文加入新证据。
- [x] `prepare_refresh`保留经过验证的tail_evidence；`select_threads`从有效证据取最新source_count用于未变判断，不能覆盖历史checked_count。缺失/无效证据回落既有选择逻辑。为本轮选择tail的楼写 `refresh_action='tail'`，其他楼按现有full/skip处理。
- [x] 运行 `python -m pytest backend/tests/test_tail_collection.py backend/tests/test_incremental_collection.py backend/tests/test_refresh_baseline.py -q`；通过后提交 `feat: preserve trusted pagination boundaries across refreshes`。

## Task 4：尾部请求、回退与恢复闭环

**Files:** `tail_collection.py`、`collector.py`、`incremental.py`、`backend/tests/test_tail_collection.py`、`backend/tests/test_access_checkpoint.py`。

**Interface:** `run_tail(plan: TailPlan, source: dict, root_row: dict, old_rows: dict[str, dict], cp: Checkpoint, client: httpx.Client, progress: dict) -> bool`。它在既有task_budget作用域内运行；True表示候选已校验且原子合并，False表示完整获取回退标记已持久化。来源/取消/租约/预算异常原样交给现有控制路径，不转换为普通回退。

- [x] 写假来源集成用例并先运行失败测试：full建立397条基线，auto刷新398条时楼中楼请求严格为19、20，结果398条且ID、作者、正文与独立完整对照一致；400→401严格为19、20、21。
- [x] run_tail每次进入调用begin_tail，从计划起始页读取；页面经fetch_replies和既有normalize_comment转换为payload，先stage。最终validate成功才将候选ID计入观察集合并promote；旧未读记录的collected_at不变。
- [x] 成功写 `tail_completed=True`、`refresh_action='tail'`、pagination_status='partial'、reply_verification='reused_unverified'，保留历史完整证据。`thread_done`识别本任务tail_completed；新任务prepare_refresh不继承本次完成标记。finish_tracking不因尾部成功将覆盖升级或刷新完整核对时间。
- [x] TailMismatch触发abandon，保存 `refresh_action='full'`、`tail_fallback_reason`、`tail_disabled=True`，清理该楼分页临时状态后从1页完整获取。候选键与 `reply:<root>:<pass>:<page>`隔离，每楼本任务最多一次策略回退。
- [x] 计数变化、错位、新ID已在别楼存在、倒序、重复页、异常空页、根身份变化逐一测试；适配器在返回页面前拒绝的invalid_response/video_identity_mismatch仅允许转入一次完整路径；完整路径仍拒绝时沿用原错误处理。source_reply_unavailable保留既有楼不可用处理，其他来源拒绝不回退、不绕过闸门。完整回退失败沿用已有partial处理。
- [x] 在候选第1页后注入中断：resume必须以新attempt从重叠页开始，保留预算，不直接续读旧尾页；在promote后注入中断，resume不得重复获取或重复加入评论。检测取消、租约和源端限制均不发布未校验候选。
- [x] 运行 `python -m pytest backend/tests/test_tail_collection.py backend/tests/test_incremental_collection.py backend/tests/test_access_checkpoint.py backend/tests/test_worker_collection_hooks.py -q` 及Ruff；提交 `feat: collect appended replies with guarded fallback`。

## Task 5：数据库交接与公开协议兼容

**Files:** `backend/app/storage/handoff.py`、`baseline.py`、`backend/tests/storage/test_tail_handoff.py`，必要时补充同目录`test_worker.py`与`test_roundtrip.py`。

- [x] 写tail_evidence经materialize→freeze_baseline往返保留的失败测试，含连续两次尾部更新；引用Task1校验函数，不另造第二套证据规则。
- [x] handoff仅白名单保存有效tail_evidence；不保存候选页、整楼有序列表或未确认attempt。继续验证partial与继承记录一致，不将historical complete误认为本轮checked_now。
- [x] baseline对证据执行版本、时间、数量、ID归属及根签名校验：旧记录无此字段正常读取，未知/无效可选证据丢弃并走完整路径，基础记录本身损坏仍按原存储错误处理。
- [x] 测试无证据的旧JSON、v1导入、v2再导出；公开枚举和目录结构不变，导出不存在内部tail_evidence及候选状态。
- [x] 在隔离 `bilibili_talks_test` 数据库验证所有权/缓存版本冲突时结果与任务终态同事务回滚，旧可读状态和正文载荷不损坏。未配置测试库导致skip不能算通过。
- [x] 运行 `python -m pytest backend/tests/storage/test_tail_handoff.py backend/tests/storage/test_handoff.py backend/tests/storage/test_worker.py backend/tests/storage/test_roundtrip.py -q`；通过后提交 `feat: persist tail evidence through database handoff`。

## Task 6：整体验收与交付

**Files:** `README.md`、当前设计/计划；正式测试文件按前五项分布。

- [x] 执行固定夹具性能断言：397→398为2次楼中楼请求，400→401为3次；证据失效回退请求数有界，小楼/full/周期到期保持原行为。比较新增ID、正文、根楼、UID、父评论及覆盖标记，不只比较总数。
- [x] 验证中间等量替换和未读正文更新不能被尾部保证发现：partial不被清除，last_full_checked_at不推进，到期后走full。连续追加不能无限延后核对。
- [x] 全量回归：`python -m pytest backend/tests -q`（含隔离数据库）、`python -m ruff check backend scripts`、`npm --prefix frontend test`、`npm --prefix frontend run build`。Windows使用npm.cmd。
- [x] 复用已授权调试视频，仅选择少量楼作真实有限对照；先确认来源闸门、无冲突采集，再建立新格式完整基线。没有真实新增时明确使用离线回放，不能将构造的旧缓存冒充真实历史。不要把既有目录样本原地覆盖。
- [x] 更新README说明启用条件、老数据首次暖基线、partial含义、完整回退及运行版本要求；记录实际测试结果和未完成约束到计划，不写排查报告入库。
- [x] 独立审阅实际diff与测试结果，修复发现的问题并重跑受影响检查。当前功能通过验收后，本地 `git merge --squash opt/comment-collection-performance` 合入最新main，使用详细单个提交并正常push，fetch后核对HEAD与origin/main相同；不改写已发布历史。

## 执行顺序与交接

按Task1→2→3→4→5→6执行。每项先验证失败用例，再实现并通过相应检查；功能分支允许中间提交，main只保留一次squash。建议在当前任务中分单元实施并审阅，避免多个执行者同时修改collector/checkpoint；独立评审可由子Agent完成。用户已确认执行，本轮实现与验收已完成。

## 实施中明确的细节

- 共享存储证据校验提取至 `backend/app/storage/tail_evidence.py`，同时核对历史计数与根签名，避免handoff与baseline重复实现。
- 候选控制写入必须保留调用者metadata引用；abandon_tail增加显式confirmed_progress参数，回退决定与abandoned标记同事务提交。原计划中分两次保存的方式已根据审阅修正。
- 完整采集仅为大于60条回复的楼保存锚点；小楼本来就不走快速路径，避免空楼证据体积和校验成本。按楼索引已有记录，不在每个楼结束时重复freeze整个工作集。
- 完整证据与checked_thread复用同一时间戳；尾部来源不可用沿用实际请求页码验证，不能硬编码第一页或起始页。
- 逐页立即验证可判定的计数、身份、顺序问题，避免明知异常仍读取所有尾页后才回退。

## 实际验收与交付边界

最终后端452项测试（含隔离PostgreSQL）通过；前端21项测试、TypeScript/Vite构建及Ruff通过。独立审阅指出的metadata引用、回退原子性、尾页不可用和逐页提前拒绝问题均已修复并复审。

用户指定剧集的两个楼完成有限真实页对照：在明确构造的旧前缀缓存上，尾部结果与完整读取的规范化记录一致，partial及历史完整核对时间保持正确。该实验不是自然新增事件，不代表整视频耗时承诺。原始证据仅存忽略目录。

本地旧worker在空闲时停止，工作集已备份；以新版worker继续本机服务，不部署公网。没有改写现有评论批次或D:/Data下游样本，没有PostgreSQL迁移。已有缓存缺少锚点时先正常完整核对需要更新的楼，再具备尾部加速资格。
