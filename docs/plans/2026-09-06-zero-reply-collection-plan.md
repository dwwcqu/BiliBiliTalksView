# 零回复楼请求优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. 用复选框记录实际完成情况。实现、独立审阅与验收已完成。

**Goal:** 在普通auto中按可信零回复观察省略详情请求，同时保留历史反证、固定核对期限、partial语义和严格核验路径。

**Architecture:** 候选与历史、模式与期限使用两个纯逻辑模块；collector在完整主列表扫描后选择零省略。工作集及原子数据库交接保存版本化内部证据，API仅公开状态级汇总计数。

**Tech Stack:** Python 3.12、HTTPX、SQLite、SQLAlchemy/PostgreSQL、React/TypeScript、pytest、Node原生测试；不新增依赖。

**Spec:** [已批准设计](2026-09-06-zero-reply-collection-design.md)，并参考[正反例验证边界](2026-09-06-zero-reply-validation-design.md)。设计已确认；实施计划已确认。

## Global Constraints

- 沿用 `opt/zero-reply-collection`；任务结束后本地squash合并main，普通push并核对远端。
- 保留主评论完整分页、详情每页20条、共享限速、小时资格、活动任务去重及尾部增量。
- 楼主及已保存回复不删除；候选计数必须是严格整数0，未知旧历史不能填false。
- auto首次无基线可full范围+observe；显式新建full、到期任务和不可信旧基线使用verify。原collect默认保持verify。
- 恢复沿用冻结策略和期限；跨期限observe仍可结束为partial，下一新建auto必须verify，不自动触发采集。
- worker间隔24小时；CLI沿用full_interval_hours。新的间隔只能提前已存在期限，不能延后；恢复不改参数。
- 只要本轮实际零省略，覆盖保持partial，不调用checked_thread、不伪造详情核验时间。
- 外层refresh_context版本保持1，公开JSON/JSONL协议和枚举不变。内部增加可选v1扩展，无需新增PostgreSQL列/迁移。
- 常规测试使用固定假来源与隔离数据库，不联系Bilibili。真实有限对照及临时脚本只放忽略目录；不写报告入库。

## 模块与数据契约

| 模块 | 变更 |
| --- | --- |
| 新增 `backend/app/comment_export/zero_reply.py` | 原始主列表观察、历史反证合并、资格校验 |
| 新增 `backend/app/comment_export/zero_schedule.py` | 冻结策略、期限选择、完成证明构建/校验 |
| `collector.py`、`incremental.py`、`refresh.py`、`backend/app/jobs/work.py`及必要的runner调用 | 入口、逐页持久化、选择顺序和恢复 |
| 新增 `backend/app/storage/zero_evidence.py`，修改handoff.py、baseline.py | 内部扩展白名单、证明绑定及旧数据降级 |
| `backend/app/api/discussions.py`、`frontend/src/api.ts`、`frontend/src/App.tsx` | 状态级计数与明确提示 |
| `backend/tests/test_zero_reply.py`、`test_zero_schedule.py`、`test_zero_collection.py` | 规则、期限、采集及中断测试 |
| `backend/tests/storage/test_zero_handoff.py`、`test_http_api.py`、`frontend/tests/api.test.mjs` | 存储、API与客户端兼容测试 |

内部状态约定：

- `zero_reply_history`：version=1、known、ever_nonzero_observed、ever_source_unavailable。known仅在本地首次观察且历史从此完整追踪时为true；旧扩展缺失/坏版本为unknown。两个反证只能单调变true，unknown中的已知反证也要保留。
- `zero_main_observation`：本任务视频/根身份、观察时间、rcount_zero、aux_zero、preview_empty、conflict。重复观察做保守合并，conflict只增不减；具体合法字段判定见Task1。
- `zero_reply_evidence`：version=1、work_id、video_id、root_id、observed_at、source=main_list、有效计数/预览结论和根身份。仅在本轮实际选择零省略后生成，历史证据不能直接复用为本轮证据。
- `zero_policy_snapshot`：version=1、work_id、requested_mode、range_mode、zero_policy、started_at、interval_hours、verification_anchor_at、verify_due_at及基线绑定。worker的work_id取job_id；CLI生成一次UUID，准备时持久化；重试不换ID。
- `zero_reply_schedule`：版本、完成时刻、冻结期限来源及本轮绑定，见Task2。导出新批次会更换export_id，证明的work_id和稳定数据绑定不能因此被假装改成另一轮次。
- 本任务临时标记 `zero_completed` 与跨任务证据分开；下次prepare_refresh清除它及本轮观察。

## Task 1：零候选、历史与反证

**Files:** 新增zero_reply.py及test_zero_reply.py。

**Interfaces:** `observe_zero(raw: dict, source: dict, observed_at: str) -> dict`；`merge_observations(previous: dict | None, incoming: dict) -> dict`；`merge_history(previous: object, *, is_new: bool, nonzero: bool, unavailable: bool) -> dict`；`can_omit(observation: dict, history: dict, *, stored_replies: int, policy: str) -> bool`。函数不修改输入，不保存任何评论或发送请求。

- [x] 先写参数化反例并确认失败：

```python
@pytest.mark.parametrize('value', [None, False, 0.0, '0', -1, 1])
def test_non_integer_or_nonzero_is_not_candidate(value):
    raw = {'rpid_str':'100', 'oid_str':'1', 'root_str':'0', 'type':1,
           'rcount':value, 'count':0, 'replies':[]}
    observation = observe_zero(raw, {'oid':'1', 'comment_type':1},
                               '2026-09-06T00:00:00Z')
    history = merge_history(None, is_new=True, nonzero=False, unavailable=False)
    assert not can_omit(observation, history, stored_replies=0, policy='observe')
```

- [x] 实现严格整数判定 `type(value) is int and value == 0`；rcount缺失不合格。辅助count存在时也必须合法整数0；replies只接受缺失/null/空list。校验原始评论ID、oid、type和根身份；来源失败由现有适配器拒绝，不能构造成功观察。
- [x] 新增正例（空/null/缺失预览）、非法ID、错误视频、非根、非空/畸形预览、辅助计数冲突、重复0后1再0测试。任何冲突在本任务内保持，置顶重复不重复统计。
- [x] 测试旧历史unknown、已有回复、历史非零和源端不可用；历史字段经多个合并不能从true变false，既有可确认反证不能因未知扩展版本丢失。known=false永不获得零省略资格。
- [x] 测试主列表读零后详情新增的反例：候选并非永远为空的证明，不能生成checked_now；根记录保存留给Task3验证。
- [x] 运行 `python -m pytest backend/tests/test_zero_reply.py -q` 及相关Ruff检查，通过后提交本单元。

## Task 2：策略冻结、固定期限与完成证明

**Files:** zero_schedule.py、test_zero_schedule.py；接口适配集中在Task3。

**Interfaces:** `decide_policy(requested_mode: str, now: str, interval_hours: int, baseline: dict | None, *, work_id: str, baseline_binding: str | None) -> dict`返回冻结快照；`validate_schedule(value: object, metadata: dict, rows: list[dict]) -> dict | None`；`complete_schedule(snapshot: dict, metadata: dict, rows: list[dict], progress: dict) -> dict | None`。时间为带时区ISO字符串，非法/未来完成时间拒绝作为加速证据。

- [x] 先写策略矩阵测试：无基线新auto=full+observe；显式full=full+verify；已有无可信证据基线=full+verify；有效未到期新版完成证明=incremental+observe；有效旧版完整证明沿用原规则；任一有效最早期限到期=full+verify。
- [x] 固定首次准备时间为anchor。有效旧证明存在时继承其anchor/due，普通新轮不得以now重新开始24小时。间隔缩短使用原anchor加较短间隔与旧due的较早者；延长间隔仍保留旧due。跨期限resume读取原快照，不调用decide_policy重新冻结。
- [x] 定义稳定完成绑定：work_id、video_id、baseline_binding、main_done、所处理全部根ID及动作、观察集合、规范化记录身份/内容摘要。使用排除export_id/schema_version及自身证明字段的规范化摘要，避免导出新UUID破坏绑定或递归散列。数据库handoff仍另外绑定正式批次digest。
- [x] complete_schedule只在progress.finished且main_done、全部楼有合法完成动作时生成；零动作必须本轮观察且候选/历史/无回复记录同时有效。详情完成、有效不可用、原规则合法复用及尾部完成分别校验，不能仅检查一个自报字符串。未完成楼、候选未提交、取消/预算/异常不产生本轮证明。
- [x] 只有完整范围实际逐楼详情处理结束才可推进原last_full_scan_completed_at并建立新due；verify整轮正常满足这一条件。observe若没有任何零省略、复用或尾部处理且详情范围实际完整，也可保留原完整证据；不得仅凭range_mode=full判断。observe省略详情时只更新新的scan证明。复制历史证明保持原work_id、时间和绑定，不冒充本轮完成。
- [x] 测试20小时后更新不移动due、25小时后恢复observe仍partial、下一新任务verify；伪造完成时刻/楼集合/摘要、混入另一视频证明均不能加速。运行 `python -m pytest backend/tests/test_zero_schedule.py -q`，通过后提交本单元。

## Task 3：入口贯通、候选落盘与采集选择

**Files:** collector.py、incremental.py、refresh.py、jobs/work.py和必要runner参数；新增test_zero_collection.py，扩展既有刷新/worker测试。

- [x] 在首次网络请求前准备并保存zero_policy_snapshot及冻结基线根ID集合；CLI复用原意图工作集，worker复用job_id/基线版本。旧恢复任务没有策略字段时使用verify，不误认成真正首次无基线auto。
- [x] 在主列表处理时调用Task1观察/历史合并；相对于冻结基线识别本地新楼。raw观察、冲突、根评论和历史标志同页提交；恢复后置顶重复不能重新初始化历史。
- [x] 正例测试：1个零楼auto无详情请求，根评论ID/UID/正文保留；N个零楼相对verify减少N次详情请求。原collect和新建full仍请求详情。假来源固定响应，请求列表可精确断言。
- [x] 主列表扫描到尾后先保留原增量复用/尾部选择；仅对原本需详情且策略observe的合格楼执行zero动作。设置zero_completed、count=0、pagination_status=partial、reply_verification=not_checked或保留合法历史含义；不调用checked_thread、不写新的详情时间。
- [x] 同时更新thread_done与finish_tracking：zero动作可结束本任务，但不能让full范围的observe冒充旧版完整详情证据；交给complete_schedule生成独立证明。prepare_refresh复制有效历史/调度证据并清除本轮zero_completed、观察和冲突集合。
- [x] 详情返回非零、实际回复记录或有效楼不可用时单调更新历史反证；普通网络失败不能设为楼不可用。缺席旧零楼不能使用历史观察跳过本轮，也不能计入零摘要。
- [x] 测试全流程首次observe→次轮incremental→到期verify、历史反证跨多轮、0→非零、非法计数/预览及同楼冲突、主楼页中断恢复、旧完成标记不能影响新full、来源限制和预算守卫不变。
- [x] 混合测试同一视频零省略+大楼尾部+普通完整读取，所有ID/UID/父关系和旧collected_at正确；实际新增等量替换的盲区仍partial。
- [x] 运行 `python -m pytest backend/tests/test_zero_collection.py backend/tests/test_zero_schedule.py backend/tests/test_incremental_collection.py backend/tests/test_tail_collection.py backend/tests/test_refresh_baseline.py -q`，通过后提交本单元。

## Task 4：持久交接、旧数据兼容和摘要绑定

**Files:** 新增storage/zero_evidence.py，修改handoff.py、baseline.py；新增storage/test_zero_handoff.py。

- [x] 写首次observe入库→冻结基线→第二轮observe入库的失败测试，验证新schedule及逐楼历史完整保留，期限和历史反证不变；再测到期verify成功后正确推进期限。
- [x] 将三个可选v1扩展加入handoff明确白名单，复用Task1/2校验；外层仍为version=1，不保存原始响应、本轮未确认候选或额外大正文。zero_evidence必须匹配该次work_id、实际观察、根身份、零动作及无回复记录。
- [x] 在handoff中独立计算本轮实际零省略的不同root数量，与候选数/历史证据数/原缓存复用数区分；将安全汇总与结果上下文一起原子保存。不能直接信任输入汇总数。
- [x] baseline恢复可选证据未知/坏版本时，保留核心评论，按依赖关系禁用优化；已知true反证不能丢弃成false。核心封装或身份损坏仍使用原StorageError。旧v1/v2文件导入无内部证明，保守处理。
- [x] 测试伪造scan完成集合、复制旧schedule冒充新轮、缺席根的zero证据、已有回复却声称零、历史反证被清除、错误视频绑定、未来时间和未知字段；不能发布与partial不一致的结果。
- [x] 测试完整current与新partial共存、缓存版本冲突、guard异常、同任务重复物化及数据库回滚；原状态与载荷保持可读。公开再导出不得泄露三个内部扩展，不新增协议枚举。
- [x] 配置隔离 `bilibili_talks_test` 后运行 `python -m pytest backend/tests/storage/test_zero_handoff.py backend/tests/storage/test_handoff.py backend/tests/storage/test_tail_handoff.py backend/tests/storage/test_roundtrip.py -q`。skip不是通过；通过后提交。

## Task 5：API摘要与页面准确提示

**Files:** api/discussions.py、frontend/src/api.ts、App.tsx；storage/test_http_api.py、frontend/tests/api.test.mjs。

- [x] 后端summary只读取对应状态已验证上下文中的汇总，添加可选zero_reply_observed_threads。没有字段或无法识别安全汇总则不输出，不用0暗示已核验；不读取全部评论正文进行计数。
- [x] 前端Summary增加可选number；存在时严格校验非负安全整数，布尔/负数/字符串/过大数字拒绝，缺失保持旧响应兼容。
- [x] SavedSummary仅在值>0时显示：“其中X个楼仅依据主列表报告零回复，尚未访问详情核验”。current和partial分别使用自身字段，原未完整核验提示保留。
- [x] 测试current=0/partial=3、旧响应缺失字段、错误类型、仅历史零证据但本轮未观察等场景；普通页面读取与重载不发POST。
- [x] 本地模拟API浏览器检查提示、双状态区分及只读重载；不自动触发真实采集。运行相关后端API测试、`npm --prefix frontend test`和`npm --prefix frontend run build`，通过后提交。

## Task 6：整体验收、真实有限对照与收尾

- [x] 对固定来源比较observe/verify：其余楼处理策略一致，精确断言详情请求差N、楼主记录不变、公开覆盖不冒充verified。将原增量已省的请求从本优化计数排除。
- [x] 运行完整 `python -m pytest backend/tests -q`（含隔离PostgreSQL）、`python -m ruff check backend scripts`、前端测试与构建。新增测试必须先验证失败，再实现到通过，不以仅复制实现分支的断言代替行为验证。
- [x] 对已授权两视频做最多24次来源请求的有限现场对照，包含解析/分页；检查闸门与后台冲突，不修改共享限速。原始结果与脚本留在忽略目录，不能把短时一致解释为永不漏变化。
- [x] 独立子Agent审阅实际代码和正反例覆盖；修复后复审及重跑受影响检查。特别复查首次例外、跨期恢复、unknown历史、周期不可后移及原子发布。
- [x] 更新README、设计和计划状态，明确auto首次的partial语义、原collect/full保持严格、旧缓存降级、页面提示及24小时非无人值守SLA。若需切换本地worker，先在空闲时停止旧进程并备份工作集，不混跑版本；不部署公网。
- [x] 功能验收后从最新main进行本地squash合并，以单个详细提交说明功能/验证/限制，正常push并fetch核对HEAD一致；不强推，不携带验证报告、凭据和采集数据。

## 执行交接

按Task1→2→3→4→5→6执行；Task1/2的纯逻辑可独立分工，collector/incremental只由一个实现者负责。每个单元完成测试和审阅再继续依赖单元。采用当前任务内分步实现与子Agent独立审阅；用户已确认并完成本计划的实现与验收。

## 实施细节与审阅修订

- 旧恢复任务无可信新快照时禁用新增零省略，保留原页码、模式及已确认的普通复用/尾部决定；不因升级重启原任务，也不为它生成虚假的新版完成证明。未知快照版本、非法full+observe、工作ID或job绑定矛盾均在使用前降级。
- 完成证明核对本轮实际观察的回复ID数量，不能只相信checked_count；baseline_digest与main_count需明确贯通handoff白名单。
- 在物化事务内比较旧可读状态的历史反证，拒绝将已有true标志清除；API统计还验证完整策略快照、任务绑定、时间及状态覆盖。
- 按楼预索引已有回复和观察ID集合，避免在大量零回复楼场景中引入平方级循环。新增test_zero_summary.py覆盖API可选摘要，不需要真实数据库。

## 实际验收与边界

后端600项测试（含隔离PostgreSQL）、前端23项测试、生产构建及Ruff通过。模拟API浏览器验收确认双状态提示、旧字段缺失兼容、只读重载和移动布局。独立审阅提出的当前观察计数、非法快照、历史反证、摘要绑定及重复扫描问题均已修复并复审。

正式判定函数完成有限真实正反例对照；测试场景明确使用模拟的首次本地历史，未将其发布到用户缓存。真实接口对照中也观察到此前零回复楼后来变为非零，并正确拒绝省略。样本一致不构成永不遗漏或整视频耗时保证。

本地服务空闲停止后已备份工作集，新API能读取原缓存；新版worker用于后续显式任务。无PostgreSQL迁移、无公网部署，不改写D:/Data导出样本。普通auto的零省略保持partial，严格full与到期核验保留，其他性能方向仍单独推进。
