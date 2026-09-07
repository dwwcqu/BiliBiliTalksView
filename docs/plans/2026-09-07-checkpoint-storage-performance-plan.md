# 检查点与入库性能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. 用复选框记录实际完成状态。用户已确认实施；A单元已通过验收，A、B单元及整体回归验收完成，按约定squash交付。

**Goal:** 高频请求/心跳不再解析大metadata，后台入库不再生成和复制导出目录，同时保持恢复、摘要和数据结果等价。

**Architecture:** A单元先实现版本化检查点布局与轻量控制接口；B单元随后提取逻辑文档校验和只读数据集，为文件入口与直接入口提供同一校验后消费接口。

**Tech Stack:** Python3.12、SQLite、HTTPX、SQLAlchemy/PostgreSQL、pytest；保留Node24前端回归，不新增依赖。

**Spec:** [已批准设计](2026-09-07-checkpoint-storage-performance-design.md)。设计已确认，本计划已确认。

## Global Constraints

- 分支 `opt/checkpoint-storage-performance`；先A验收，再B，不跳过依赖单元。
- 来源请求策略、每页20条、共享限速、小时资格、零回复/尾部算法及公开协议不变。
- 不能降低持久化频率、关闭SQLite同步、跳过校验或合并不同状态冒充提速。
- 请求预算保留PostgreSQL及SQLite两层；恢复取较大已用量和较小适用上限，不退款、不重置。
- 格式版本不等于checkpoint_revision；只读不迁移，未知高版本任何写入前拒绝。
- 文件输入安全、扩展字段、原JSON标量表示和逻辑行序保持；现有canonical digest算法不更换。
- 独立评论主记录只存一份，视图优先用引用；仅相同规范编码可共享表示，不能因Python对象相等而抹去合法源视图中1与1.0等差别。
- 所有嵌套元数据及handoff证据与数据集同一私有快照，不能暴露可变内部引用。
- 常规测试不访问Bilibili；性能日志和实测材料仅在忽略目录，不作为报告入库。
- 同一功能完成后本地squash合入最新main，详细单提交并正常push，不改写已发布历史。

## 文件职责与依赖

| 新增或修改文件 | 职责 |
| --- | --- |
| 新增 `backend/app/comment_export/checkpoint_layout.py` | 版本探测、事务内初始化/迁移、分离与重组状态 |
| `checkpoint.py`、`request_budget.py`、`backend/app/jobs/ownership.py` | 对外兼容、专用扣费、轻量心跳 |
| 新增 `backend/app/comment_export/layout.py` | 从export.py提取排序/昵称/目录命名纯函数，export.py保留兼容导出 |
| 新增 `dataset_validation.py`、修改validation.py | 共享逻辑文档校验与保留物理文件安全检查 |
| 新增 `dataset.py`、`dataset_builder.py`、修改export.py | 只读数据集、采集数据推导、显式文件写出 |
| `backend/app/storage/frozen.py`、importer.py、handoff.py、exporter.py、jobs/work.py | 文件适配、直接数据集入库及原子发布 |
| 新增测试文件见各Task | 布局、控制、等价性、不可变性及性能结构断言 |

依赖方向：layout/contract为底层；dataset_validation只依赖底层；dataset依赖逻辑校验；dataset_builder依赖dataset与layout；export仅负责调用builder与写出。避免validation→export→dataset→validation循环导入。

## Task 1：版本化检查点布局及迁移（A1）

**Files:** checkpoint_layout.py、checkpoint.py；新增`backend/tests/test_checkpoint_layout.py`。

**接口约定：** `detect_layout(connection)->int`只读返回1/2，未知标记报ContractError；`initialize_v2(connection)`与`migrate_v1(connection)`由持锁调用者在既有事务内执行，不自行commit。v2的state键为checkpoint_format=2、metadata、progress、request_control、progress_summary、progress_metadata_attached；保留comments/pages/tail表。

- [x] 写旧版夹具：直接用sqlite3创建旧表，写入有/无progress.metadata、计数、revision、尾候选；不借新构造器生成所谓旧格式。
- [x] 写失败测试：读旧版不写入；升级后评论/页/候选/预算相同；中途注入异常整个升级回滚；未知版本3在任何CREATE/UPDATE前拒绝。检查首次空数据库与损坏数据库不能混为一谈。
- [x] 从旧progress拆出requests/max_requests/checkpoint_revision/attempt到request_control；保持字段原存在性。progress移除这些字段和metadata，内部attached标志保留其原是否附带metadata。不得给空progress凭空补字段。
- [x] 重组时使用权威metadata行；同身份旧嵌入副本过时允许规范重组，身份冲突或权威数据损坏拒绝迁移，不能将候选副本提升为权威。格式标记最后写入，逻辑revision不变。
- [x] 审计CLI、worker、恢复、可用性和直接Checkpoint使用的写锁；所有可变入口先检测版本并在工作集排他保护下迁移。只读函数不通过可变构造器触发迁移。
- [x] 运行 `python -m pytest backend/tests/test_checkpoint_layout.py backend/tests/test_checkpoint.py backend/tests/test_tail_checkpoint.py -q`；通过后提交A1。

## Task 2：专用请求扣费和兼容读写（A2）

**Files:** checkpoint.py、request_budget.py；新增`backend/tests/test_checkpoint_control.py`，更新相关访问/恢复测试。

**接口：** `Checkpoint.read_request_control()->dict`；`Checkpoint.reserve_request(attempt:dict,max_requests:int)->dict`在单个SQLite事务读取、校验并扣费，返回控制快照。存储层预算异常用ContractError，由request_budget转换为原CollectionStopped语义。

- [x] 写结构性失败测试，metadata使用明显大标记，跟踪SQL与解析调用：

```python
before = checkpoint.read_request_control().get('requests', 0)
result = checkpoint.reserve_request({'phase':'main'}, 10)
assert result['requests'] == before + 1
assert checkpoint.read_request_control()['requests'] == before + 1
# 测试spy另外断言本次操作没有读取、序列化或UPDATE metadata。
```

- [x] reserve保持先检查上限再持久扣费，请求诊断绑定该事务读取的实际revision，不信任调用者过时副本；call_limit仍在外层先检查。PG守卫/扣费顺序不变，PG已扣而SQLite失败的恢复仍取max，不引入两库伪原子事务或自动退款。
- [x] get_progress/read_checkpoint重组完整兼容结构；set_progress/commit_page/save_metadata拆分写入但同事务提交。只同步调用者控制字段，不替换已有metadata对象引用。
- [x] 尾部begin/stage只写控制和候选，promote首次推进revision，重复promote不推进；abandon的已确认回退与标志仍原子，revision不变。普通页新增推进，重复页和扣费不推进。
- [x] 测试扣费后崩溃重开、较旧调用者progress不能回退计数、预算收紧、metadata副本不再物理重复、只读一致快照、候选字段不泄露及恢复证明仍匹配revision。
- [x] 运行检查点/访问/恢复/尾部测试及Ruff，确认A2后提交。不得将完整get_progress藏在新的高频接口里。

## Task 3：轻量心跳摘要与A单元验收（A3）

**Files:** checkpoint_layout.py、checkpoint.py、jobs/ownership.py；新增`backend/tests/test_checkpoint_summary.py`及worker测试。

- [x] 在升级或首次初始化时创建摘要；所有影响已确认metadata/评论或阶段的写入同事务维护。计数公式严格沿用设计：comments表行数、len(_threads)、complete历史状态数、unavailable真值数，以及finished/main_done阶段优先级。
- [x] 新增轻量`read_progress_summary(path)->dict`，一个只读事务绑定request_control和progress_summary；worker续租使用此接口，不解析metadata。旧格式允许原只读兼容路径，不写入迁移。
- [x] 摘要缺失/损坏由持锁写入者重建；轻量读明确返回摘要不可用，不能假报0。仅展示摘要失败时续租可继续，旧progress更新时间不能伪更新；安全控制字段无效仍停止。
- [x] 测试仅progress阶段改变也更新phase；候选页不计入评论；并发读看不到半页状态；普通扣费不重算全楼；增大metadata体积不会增加心跳解析正文体积。revision测试与A2共用原语义。
- [x] 运行完整后端（含隔离PostgreSQL）验证A；在固定旧工作集副本上验证升级、读取和恢复。通过后才开始B，禁止在唯一真实工作集上试验迁移。

## Task 4：提取逻辑文档校验（B1）

**Files:** layout.py、dataset_validation.py、validation.py、export.py；新增`backend/tests/test_dataset_validation.py`。

**接口：** `validate_documents(documents)->dict`接收相对POSIX路径到JSON值/JSONL行序列的只读逻辑映射，返回已验证manifest的独立值；README逻辑值为空字符串。物理文件安全不在此函数内替代。

- [x] 先做对照测试：现有合法v1/v2批次加载为逻辑文档后通过；缺页、额外文件、用户/楼记录不一致、错误排序、计数、身份、隐藏缺口及非法JSON同样拒绝。
- [x] 提取order/nickname/目录命名到layout，export保留旧导入路径的兼容名字；validation不再反向依赖export构建流程。
- [x] 将既有内容校验移入validate_documents；validate_batch继续检查路径、符号链接、指针/复制一致性、版本和实际文件集合，再调用共享校验。文件输入不重新推导覆盖或重写扩展来使错误变合法。
- [x] 对旧验证器建立固定夹具行为对照，包括NUL、孤立代理字符、未知作者、缺失父节点、扩展字段和允许的数值表示；不要简化成仅检查schema。
- [x] 运行现有导出/协议/文件发布测试及新逻辑校验测试，确认没有循环导入后提交B1。

## Task 5：只读数据集与摘要等价（B2）

**Files:** dataset.py、dataset_builder.py、export.py、storage/frozen.py；新增`backend/tests/test_validated_dataset.py`和`backend/tests/storage/test_dataset_digest.py`。

**接口：** `ValidatedDataset.from_documents(documents, *, evidence=None)`冻结私有输入并运行逻辑校验；manifest/digest属性只读；`read_document(path)`、`iter_comments(root_id)`返回只读值或独立副本；`iter_documents()`按规范路径顺序提供逻辑视图。`build_dataset(records,metadata)`用于采集构建，`write_dataset(dataset,destination)`仅显式导出时使用。

- [x] 从现有build_batch拆出构建逻辑，固定相同export_id/schema/时间/路径/行序后生成相同文档。文件适配器直接保留已验证源文档，不调用builder重建。
- [x] 冻结涵盖评论、manifest、索引、扩展及handoff证据。测试修改原始嵌套输入和返回视图不会改变后续读取、摘要或物化；不能仅给外层dataclass加frozen。
- [x] 记录主体一次存储，普通楼/用户视图以引用表达；源视图仅在规范编码完全相同时共享。合法但编码不同的源值必须保留其对应视图表示，不以dict的数值宽松相等丢弃差异。
- [x] 实现与现canonical_digest相同的逻辑编码：按路径排序的[路径,值]数组、JSONL原行序、空README、ensure_ascii、sort_keys、紧凑分隔和非有限数拒绝。可流式编码，但必须与原算法逐例对照。
- [x] 对照v1/v2、扩展字段、1/1.0、Unicode转义、unclassified行序、不同输入键插入顺序；相同固定批次摘要完全相同，不同受保护文档变化必须改变摘要。
- [x] 保留freeze_batch对文件安全和可变指针的处理，返回包装新数据集的兼容适配器；已使用FrozenBatch的调用者由下一单元逐步接入。通过相关单元测试后提交B2。

## Task 6：后台直接入库和幂等事务（B3）

**Files:** storage/importer.py、handoff.py、exporter.py、frozen.py、jobs/work.py；新增`backend/tests/storage/test_direct_dataset.py`。

- [x] 先写失败测试：将worker路径的build_batch、copytree和文件读回替换为抛错spy，直接数据集仍能入库；文件导入仍执行对应安全检查，不能靠禁用整个冻结环节通过。
- [x] 导入器从统一数据集读取manifest、楼/用户元数据和记录，复用payload去重、成员写入和回收。原分块文件CLI与原子worker两种事务模式均保留。
- [x] 工作集一次一致读取后建立私有数据及证据快照；prepare_handoff消费这份快照，不再从原可变metadata补读证明。校验视频、work/job、基线、所有记录、尾部/零/周期和历史反证，实际数据绑定不弱化。
- [x] 新物化操作继续要求基线版本相等；已有成功收据按原video/export_id/digest、完整context、生命周期及指针规则重放。测试缓存版本已推进但同收据重试仍幂等，不重复发布/回收；不同context或新旧批次冲突仍拒绝。
- [x] 测试guard异常/取消/租约和并发基线变化全部回滚，旧结果可读；数据、指针、上下文和任务终态保持同事务。禁止重新发布已回收旧状态或提前生成用户文件。
- [x] 同一固定批次经文件/直接入口，数据库查询、状态、再导出和内部证明一致；使用隔离bilibili_talks_test，skip不算通过。通过后提交B3。

## Task 7：性能对照、审阅与交付

- [x] 固定虚构样本覆盖小/中/大规模、零楼居多、单个大楼、混合未知作者/缺口；设置相同数据、时间、export_id和策略，排除来源网络等待。
- [x] 分阶段测量扣费、页提交、候选、心跳、构建、文件处理和SQL提交；多轮中位数/尾部耗时及峰值内存仅记录在忽略目录。CI使用确定的读写次数/字节/文件调用断言，不使用不稳定墙钟阈值。
- [x] 全量 `python -m pytest backend/tests -q`（含隔离数据库）、`python -m ruff check backend scripts`、前端测试和构建。必须有旧格式迁移中断回滚、未知版本、双预算间隙、嵌套不可变、摘要等价、合法收据重放及所有已批准反例。
- [x] 子Agent独立审阅A和B的实际diff与测试；修复后定向复审，不将性能理由用于放宽数据校验。
- [x] 更新README和设计/计划完成状态，记录真实测量范围及限制，不承诺整个视频同比提速。若要切换本地服务，先空闲停写并备份，再用新版；无公网部署，不覆盖D:/Data样本。
- [x] 全功能验收后在最新main本地squash，单个详细提交并正常push，fetch确认HEAD一致。临时基线、备份、测量报告和原始响应不得入库。

## 执行顺序与验收门

Task1→2→3完成A；A通过后执行4→5→6完成B，再执行7整体交付。相同底层文件由单一实现者负责，独立审阅可以并行；不在实现A时同时改造B依赖的文件接口。每个任务先写行为失败测试、确认失败原因，再实现和验证。用户确认本计划后开始代码实施。

## A单元交付记录

A单元完整后端634项测试及Ruff通过，旧工作集一致性备份副本迁移保持记录、权威metadata和控制字段一致。迁移需要真实持有.collect.lock，只读导出不迁移；派生摘要错误不影响权威读取，worker恢复保留更严格的PG上限。固定虚构工作集的扣费和心跳结构断言通过，具体性能样本仅存忽略目录，不等同于整视频加速承诺。


## B单元及整体交付记录

共享逻辑校验、递归不可变数据集及worker直接入库已完成，独立审阅的v1路径冲突和摘要兼容问题已修复。原源扩展、标量类型、文件安全检查及合法收据重放规则保留。完整后端682项通过，补充跨入口数据库及再导出一致性1项通过；前端23项、构建、Ruff和Compose配置验证通过。无测试跳过数据库验证。

固定合成样本覆盖小中大规模、单大楼及未知作者/缺口；测量扣费、页提交、候选、心跳、构建/文件处理、SQL物化边界及额外Python峰值内存。SQL只作小样本边界观测，不推导大型事务或整视频提速比例；原始测量材料仅在忽略目录。没有新增来源请求。已有工作集一致性备份后恢复本地API和worker，健康/缓存/页面只读检查通过；生产部署与D:/Data样本不在本次变更中。
