# 分析输入与用户画像存储设计

日期：2026-09-06。版本：0.3。用户已确认下列目录结构和最终画像 JSON 形式；本文件细化目录职责、版本边界与当前交付范围。

## 已确认目录

```text
data/analysis/
└── bilibili-video-<aid>/
    ├── sessions/
    ├── inputs/
    │   └── <export_id>/
    │       └── ...
    └── runs/
        └── <analysis_run_id>/
            ├── run.json
            ├── context/
            ├── intermediate/
            └── users/
                └── <UID>.json
```

视频身份取自 manifest.video_id/source.aid，UID 取自 author.uid，不由文件夹昵称推断。analysis_run_id 由分析模块生成，不等于 export_id，也不等于 Claude session_id。所有 ID 保持各自类型，数字身份以字符串保存。

## 当前交付范围

实现输入准备与运行目录登记：严格消费上游导出、保存固定评论输入、冻结本轮 README 与提供的规则、返回准备状态。首期使用脚本入口和可调用 Python 模块；不添加网页、不调用 CLI 模型、不生成虚假的最终画像、不启动 Agent 会话。

inputs、runs 及 sessions 位于同一视频分析根下。sessions 目录在此阶段预留；实际主／子会话 ID 注册由执行模块负责，输入准备不能伪造已创建的会话。

## inputs：固定评论输入

按 export_id 保存完整验证后的上游元数据、JSONL 及其目录关系。同一批次的评论和元数据只能有一个有效副本；已有副本复用前检查记录的内容指纹。相同 export_id 的正文、关系或元数据变化视为输入冲突，不覆盖旧数据。

README 生命周期独立：复制时可保留首次接收的 README 作为来源材料，但它不作为该 export_id 下后续运行的唯一背景依据。每轮运行将本次接收的 README 单独写入 context/README.md，Agent 读取后者。复用 inputs 时，正文等不变而 README 更新是允许的新分析配置，不修改旧 inputs 的 README。

输入语义指纹采用分析侧 analysis-exact-json-v1：Decimal 保留所有有限 JSON 数值精度，以带类型的规范树计算摘要；JSONL 只按物理 LF 分行，数组／行序保留，对象键排序，README 排除。两种视图也以精确数值逐对象核对。字段版本、路径和 export_id 都参与。共享数据库 canonical_digest 保持原算法，冻结流程通过可选 digest_function 回调选择分析摘要，默认数据库调用不变。该偏离原复用计划的调整用于避免浮点舍入碰撞和有限 1e400 被误拒绝，算法标识保存于 run.json。原文件仍按字节保存，复制与再次读取用独立字节摘要检查完整性。上下文指纹按相对文件名和实际字节计算，覆盖本轮 README 及提供的规则；不同文件名即不同上下文。指纹不加入上游 manifest 或评论对象。

固定输入在同文件系统准备区完成完整校验后发布。只以完整临时目录 rename 到不存在的 inputs/<export_id> 为提交点；失败垃圾不作为可恢复输入。该目录保持导出文件集合，不在其中添加下游索引或标记破坏上游跨文件校验。已存在输入必须重新验证结构和语义指纹，损坏报 stored_input_invalid，语义不同报 input_conflict，均不覆盖。原始上游 current 改变不影响固定输入。

## runs：每轮分析上下文与状态

每次新的有效分析配置对应独立运行。run.json 至少表达：分析记录版本、analysis_run_id、video_id、export_id、输入位置与指纹、上下文指纹、创建时间、覆盖信息、准备状态及理由、已有产物引用。

此阶段只表达准备状态，不把 ready 写成分析 completed：

- ready：输入及本轮提供的上下文已固定，符合准备策略；不表示角色配置齐全、模型调用获准或分析已完成。
- waiting_policy：输入有效，但为 partial，未指定允许部分数据；保留输入供审阅，不允许自动模型分析。
- no_analyzable_users：合法输入没有已知 UID 的可分析对象；不创建空壳用户画像。

导出不存在、格式错误、批次刷新超限、上下文文件读取失败、存储失败等为明确错误，不写入成功运行记录。具体错误码延续交接契约；分析侧增加的错误不得混入上游 coverage.reasons。

同视频输入准备使用跨进程文件锁串行写入，固定最多等待 5 秒，每 50 毫秒非阻塞重试，超限返回 lock_timeout。使用 OS 句柄锁，不删除锁文件抢锁；发布、验证既有目录和复用判断均在锁内完成。

准备请求使用规范 JSON 对象，字段固定为 preparation_version="1.0.0"、video_id、export_id、input_fingerprint、context_fingerprint、scope="whole_video"、allow_partial。UTF-8、对象键排序、无无意义空白编码后计算 SHA-256，analysis_run_id 固定为 prep-<64位小写摘要>。这只是输入准备去重键，不是未来模型执行／画像结果缓存键；后者必须纳入模型、供应商和实际执行设置。

不维护独立运行索引。完整的 run.json、context、空 intermediate/users 在 runs 同盘临时目录准备，最后 rename 至 runs/<analysis_run_id>，此目录发布是唯一提交点。若崩溃发生在 rename 前，只有临时垃圾；发生在 rename 后，重试可按确定性 ID 找到同一目录并验证。inputs 已发布但 run 未发布时，允许保留并在重试中复用 inputs。

已有运行必须检查 run.json 的身份、准备请求及指纹、输入依赖和 context 实际内容后才返回；损坏报 stored_run_invalid，不返回旧 ready、不静默新建替代。准备元记录不因未来执行状态改变而更新；后续执行记录应单独保存，避免准备复用重置分析状态。本阶段不实现显式 force 重算或最新画像索引。

准备状态优先级固定：partial 且未 allow_partial 为 waiting_policy；其余已知 UID 为零为 no_analyzable_users；否则 ready。缺失／损坏、锁或存储错误优先于这些状态，不能用 no_analyzable_users 掩盖输入错误。

创建任何分析目录前，resolve 源容器和目标根并拒绝相同、祖先或后代关系，返回 invalid_storage_root；两个同级独立目录允许。目标内既有路径需检查不通过符号链接／junction 越界。删除只限本次创建的临时目录并先校验实际路径；最终输入与运行目录不递归清理。

context/README.md 是本轮实际背景；额外规则文件按固定映射保存：键为单一文件名，拒绝空名、分隔符、控制字符、Windows 保留名、尾空格／句点及大小写折叠后的重名；README.md 为保留名。不得在不同系统上把同一映射解释成覆盖或不同文件。所有上下文 UTF-8 可读并生成内容指纹，运行期间只读。规则完整性由调用方按任务阶段明确，准备模块不能声称少数文件已构成完整可执行 Agent 配置。

## intermediate 和 users

intermediate 保存用户初步分析、楼内分析和综合分析的中间产物，按任务分离；该阶段只建立目录，不创建分析内容。

最终 users/<UID>.json 由后续执行模块在语义校验成功后原子写入，昵称存内容中，不参与文件名。画像至少关联 video_id、UID、export_id、analysis_run_id、规则与背景版本、八方向观察、证据评论 ID、覆盖限制和结果状态；字段的完整 JSON Schema 随执行模块设计，不在输入阶段虚构固定值。

部分完成或失败产物不得自动成为最新有效结果；未来最新索引由通过验收的最终结果更新，不以最新目录时间推断。历史运行、评论证据和最终画像不自动过期，备份及手动清理机制另行实施。不得删除仍被运行引用的 inputs。

## 验收与既有约束

真实 partial 样例可用于本地读取及固定输入验收，不能证明楼内上下文分析质量。自动测试采用合成夹具；不依赖 D 盘真实目录、不访问模型或 Bilibili。

上游 CommentExport 1.0.0 与 2.0.0 均不改动，按声明版本读取；README 非空仅作为分析阶段差异。遵守 current 的整批读取重试规则。所有输入读取完成、校验及持久化成功之前不允许任何 Agent 调用。

相关：[交接契约](2026-09-05-collection-analysis-handoff-design.md)、[会话管理](2026-09-05-claude-agent-management-design.md)、[当前导出协议](../references/comment-export-protocol-v2.md)。

## 当前主线复用与后续执行边界

分析 inputs 为长期文件副本；storage.frozen.FrozenBatch 为临时上下文；PostgreSQL state_id 为可过期数据库状态，三者不可互相代替。准备过程中在 FrozenBatch 退出前完成持久发布，同时冻结实际 README，不对临时路径生成长期引用。

准备去重只复用本阶段输入和 context。后续模型、供应商或执行参数变化时，应建立独立完整分析运行并引用 prepared_run_id，不能把不同模型结果共同写入一个 prep-<digest>/users。原计划的 analysis_run_id 字段在准备阶段表示准备运行 ID，最终运行拥有自己的 ID；对外字段的具体兼容方案在执行阶段冻结。

已有 sessions 目录继续预留：默认复用同视频成员的要求保持不变，但旧成员拒绝新规则、配置不兼容或历史丢失必须转为阻塞／待处理，不通过复制会话绕过。当前不能将跨进程 ID 恢复试验扩展成永久可恢复或规则热更新保证。

## 当前实现状态

文件输入准备模块已实现，代码位于 backend/app/analysis_input，CLI 见 README。保持源数据只读、v1/v2 校验、非空背景、固定副本、确定性准备 ID、跨进程锁、已有数据损坏检查和无模型授权标记。sessions/intermediate/users 仅预留目录，不代表 Agent 或画像已创建。数据库自动桥接及最终模型输出仍为后续阶段。

## 后续模型任务输入保存

按 [AnalysisInput 2.0.0](../references/analysis-input-protocol-v2.md)，完整分析运行保存 tasks/<task_id>/input.json 和 manifests/<manifest_id>/run-manifest.json 及相应索引。它们由现已实现的离线 analysis_packets 模块生成，不写入不可变 inputs 或复用准备运行冒充完整执行；analysis_input 仍仅准备来源，不生成任务包。既有 intermediate 和 users 目录职责不变。

## 已固定的最终画像文件契约

最终 users/<UID>.json 采用 [AnalysisOutput 1.0.0](../references/analysis-output-protocol-v1.md) 的 UserProfile。未完成候选保存 intermediate/profiles/<UID>/<candidate_id>.json，不冒充最终文件。已接受任务结果与验收回执独立发布，画像引用其冻结路径／摘要；背景规则和执行来源由程序填写。该输出协议的本地接受器和画像保存已实现于 analysis_results；真实CLI执行尚未接入，不能把合成测试产物当成真实画像。

## 已登记成员运行证明

投递闭环新增 runs/<run_id>/deliveries/<task_id>/<attempt_id>/，保存本次完整投递、受管原生流、调用记录及 delivery-proof.json。已接受回执将其摘要和原生 task_result 摘要绑定，重读时发现篡改必须拒绝。用户汇总任务验收后触发已有画像保存规则；合成闭环验证不等于已生成真实视频画像。详情见 [投递计划](2026-09-07-agent-dispatch-plan.md)。

## 团队与CLI历史的持久目录

原生初始化保存 `sessions/team/intent.json`，每个成员的创建/恢复证明置于 `sessions/team/members/<member_id>/`；只有全部通过才发布 team.json。旧空主会话恢复意图归档在 history/，恢复前重验无工具/成员活动。CLI自身历史用 `sessions/claude-state/` 隔离，稳定路径必须与后续投递一致；不等于永久保留保证，清理/备份仍需策略。

已创建但握手未通过或被CLI拒绝恢复的ID只作为待处理诊断状态保留，不能被bind_team或画像发布器当作可用成员。费用已发生但未通过业务验收的记录同样保存。模型响应、历史和令牌不进入Git；令牌不写入程序的请求/团队记录。
