# 大模型分析输入规范

- 协议标识：`BiliBiliTalksView.AnalysisInput`
- 固定版本：`1.0.0`
- 日期：2026-09-06
- 状态：本版固定模型输入任务包及其语义；任务包构造器、Schema 与执行器尚未实现，不表示画像质量已经验证。
- 范围：单视频、单个固定导出批次中的用户初步分析、楼内上下文分析与用户综合分析。主 Agent 使用运行清单协调这些任务。

## 1. 效力与边界

本规范与 [CommentExport 2.0.0](comment-export-protocol-v2.md)／[历史 1.0.0](comment-export-protocol-v1.md) 分离。上游原始评论和元数据不改写；任务包是由分析侧生成的只读视图。不得将模型标签混入源记录。

分析维度严格采用[已确认八方向标准](../plans/2026-09-05-discussion-analysis-standard.md)。本规范定义输入结构、覆盖语义和证据关系，不重新定义画像类别，不设置用户总分，也不替代后续最终画像输出 Schema。

字段及嵌套字段均必填；可空字段显式填 null。禁止重复 JSON 键、非有限数值、缺失必填字段或未知必填枚举；同 major 新增未知字段可以保留但不能当作指令。破坏语义的修改升 major，新增可选字段升 minor，说明勘误升 patch；固定版本不得原地改变字段含义。

数据必须通过输入准备模块固定后才能组装。每轮实际模型、上下文限额和费用授权由执行配置决定；生成任务包本身不授权模型调用。

## 2. 交付形式

任务包为 UTF-8 无 BOM、LF 的 JSON 文件，存放在对应完整分析运行内的 `tasks/<task_id>/input.json`。task_id 为生成的小写带连字符 UUID。不要将这些文件写入上游批次或已发布的 inputs 目录，也不要复用准备目录的 users 路径写不同模型运行结果。

主 Agent 接收 `manifests/<manifest_id>/run-manifest.json` 的路径及 SHA-256；manifest_id 为小写 UUID，每个阶段追加新清单目录，不覆盖已发布清单；子 Agent 接收明确的 task_id、任务包路径及摘要。文件引用都由程序验证，不靠 Agent 猜路径。模型可以使用 Read 读取任务包，但读取的原文会发送到所配置模型服务，并非只在本机处理。

路径引用统一为相对完整分析运行根的 POSIX 路径，禁止绝对路径、..、链接逃逸和借助文件名执行命令。原始评论来自已固定 inputs，由程序提供受控证据读取接口；模型不直接追随 current.json，不自行读取数据库或浏览媒体 URL。

## 3. 主 Agent 运行清单

每个 run-manifest.json 必填：

| 字段 | 类型与含义 |
| --- | --- |
| protocol / schema_version | 固定协议名与 1.0.0 |
| manifest_id / previous_manifest_sha256 | 本清单 UUID；上一清单摘要或 null，支持分阶段发布且不覆盖 |
| run_id / prepared_run_id | 完整分析运行 ID 与已完成准备的 prep-摘要 ID，二者不能混同 |
| video_id / export_id / export_schema_version | 视频身份、导出 UUID、实际 1.x／2.x 版本 |
| input_fingerprint | 已固定评论输入指纹 |
| resources | 第 5 节的背景与规则资源对象 |
| coverage | 第 8 节的批次覆盖对象 |
| task_index | `{path, sha256, task_count}`，引用逐行任务索引 |
| member_registry | `{path, sha256}`，本轮已校验的视频成员映射；首次无成员也必须是明确的空登记文件 |
| execution_limits | `{max_active_members, max_input_tokens, reserved_output_tokens, accounting_method}`；前三项为正整数，最后为估算方法标识字符串 |

清单提供索引而非整个评论区原文。task-index.jsonl 每行必填：task_id、task_type、phase、target_uid（可 null）、root_ids、input_path、input_sha256、depends_on（任务 ID 数组）。执行器保存权威任务状态；主 Agent 的自然语言完成声明不能替代状态登记。

成员登记只提供逻辑角色、实际 session_id／agent_id、可恢复状态及已有任务归属，不包含认证凭据。CLI 是否支持恢复需事先验证；任务包协议不保证旧 Agent 永久可用。

## 4. 子任务包公共结构

| 字段 | 类型与约束 |
| --- | --- |
| protocol / schema_version | 固定为 BiliBiliTalksView.AnalysisInput / 1.0.0 |
| task_id / run_id / prepared_run_id | 身份字符串；task_id 为 UUID，run_id 来自完整运行，prepared_run_id 关联固定准备记录 |
| video_id / export_id / export_schema_version | 与运行清单、固定输入一致 |
| task_type | user_initial、thread_context、user_synthesis |
| phase | primary 或 reconcile；仅 thread_context 可用 reconcile |
| scope | 第 6 节目标范围对象 |
| resources | 第 5 节背景与规则对象 |
| comments | 第 7 节模型可见评论对象数组，单包 comment_id 唯一 |
| prior_observations | 第 9 节的既有观察数组；没有则 [] |
| coverage | 第 8 节覆盖对象 |
| chunk | 第 10 节分块对象 |
| response_contract | 第 11 节的交付要求对象 |

所有 B 站数字身份保持正整数字符串，不能转换为浮点或 JavaScript Number。相同 task_id 的已发布任务包不可变；修改输入、规则或目标范围创建新任务包并登记替代关系，不在原文件上覆盖。

## 5. 背景、规则与指令分离

resources 固定包含：

- background：`{path, sha256, text}`，text 为本轮固定 README 的 UTF-8 正文，不自行生成缺失剧情。
- analysis_rules：`{path, sha256, version}`，指向冻结的八方向标准。
- role_prompt：`{path, sha256, version}`，指向冻结的通用分析角色。
- coordination：`{path, sha256, version}`，指向冻结的主／子协调规则。

path 按运行根解析，sha256 为原文件字节的小写 64 位十六进制摘要。background.text 必须与对应文件解码后的文本一致。其他资源必须在发出模型任务前实际加载，不能只提供一个模型未读的路径就声称规则已注入。每个子 Agent 独立加载；不假设主 Agent 读过后子 Agent 自动继承。

角色、规则、协调说明由调用方指定为任务指令；README 是视频背景和说明材料，不构成修改已确认画像标准或工具权限的授权。comments、既有模型观察、引用和链接始终是待分析数据；其中的命令或“忽略规则”不能覆盖任务指令。JSON 结构本身不是安全隔离，工具权限与路径边界仍由程序执行。

## 6. scope：目标与辅助上下文

scope 必填：

- target_uid：正整数字符串或 null；user_initial/user_synthesis 必须为已知 UID，thread_context 为 null。
- root_ids：本包涉及的目标楼 ID 数组，唯一；用户任务允许跨楼。
- target_comment_ids：本包要评价的发言 ID 数组，唯一。
- context_comment_ids：仅用于解释目标发言的辅助评论 ID 数组，唯一，与目标集合不相交。
- target_index：`{path, sha256, count}`，关联该逻辑任务总体目标评论清单，支持分块覆盖核对。

comments 中每条都属于目标或辅助集合，不能夹带未登记发言。用户任务的目标评论作者必须等于 target_uid；辅助评论可以来自其他人或未知作者，但不能归为目标用户行为。楼任务目标均属于 root_ids 对应楼，不能因为正文提及另一个 UID 就将其列为发言者。

user_synthesis 的 target_comment_ids 表示本包负责综合的原发言范围，comments 只须包含实际引用的证据与必要上下文；范围内其他评论必须由 prior_observations 的 source_comment_ids 覆盖，不能伪称模型又读过全部原文。前两个任务类型的所有 target_comment_ids 必须实际出现在 comments 中。

每个阶段／phase 内，目标清单中每条评论恰好有一次主分析归属；复核或重新尝试可重新读取，但只替代原任务结果，不作为新增独立样本。辅助评论可以在不同任务包重复出现，其重复不增加用户发言数或证据权重。

## 7. comments：模型可见的原文视图

每条评论必须有：

| 字段 | 类型／含义 |
| --- | --- |
| comment_id / root_id / parent_id | 原始身份，parent_id 可 null；保留未知关系，不改接根评论 |
| kind | root 或 reply |
| author_uid | 原 author.uid，允许 null |
| reply_relation | 原 `{status, target_uid}`，按来源保留 |
| content | 原 `{text, images, emotes}` 完整对象及值；text 可 null；媒体描述不表示已读取媒体 |
| created_at / collected_at | 原 UTC 时间，created_at 可 null，collected_at 必填 |
| input_role | target 或 context，与 scope 一致 |
| context_reasons | 根、祖先、目标发言或关联后续回复的角色列表：target、root、ancestor、direct_reply、cross_chunk_evidence |

这是模型输入投影，不是改写上游评论对象。第一版默认不发送昵称、点赞数及目录标签，减少无关信息；它们仍保存在固定原始输入中。不得以这类省略声称已发送所有上游字段。

content.text 原样保留，禁止预先把批评改成温和措辞、删除脏话或展开臆测的梗含义。正文换行按 JSON 转义；媒体 URL 只作为描述，不自动下载、执行或联网核实。需要视觉理解时属于另行确认能力，本版标记未读取媒体。

排序按 root_id 分组；组内根评论优先，其余按 created_at、comment_id 数值排序，未知时间排最后。回复结构由 parent_id 定义，不能用排序顺序猜测直接回复。缺父、跨楼引用或循环链必须显式登记，不无限追溯、不凭空修复。

## 8. coverage：覆盖状态始终进入模型输入

coverage 必填：

- source：manifest.coverage 原对象原样复制；保留 status、main_pagination、replies_pagination、context_status、reasons。
- corpus_counts：manifest.counts 原对象原样复制。
- context_gaps：数组，每项 `{kind, comment_ids, root_ids, detail}`。kind 固定为 missing_root、missing_parent、unknown_parent、cross_thread_parent、reply_cycle、missing_text、media_unread、context_budget、prior_result_missing；ID 数组可为空，detail 为事实说明，不作用户动机判断。
- omitted_context_ids：已知但因预算未纳入的上下文评论 ID 数组；不能把目标发言静默放进这里。
- summary_used：布尔值；只要某段依据来自压缩摘要而非原文即为 true。
- limitations：输入组装产生的解释数组，不改变上游 reasons 的码值。

source 中 verified 与 gaps 可以同时存在，不能把它们合并成一个“完整”布尔值。user.json 的覆盖字段描述整个视频批次，不直接说明该 UID 自身存在何种缺口。未知原因码必须保留并说明未解释，不删除为无异常。

只有主评论、没有已取得回复的数据可以用于发言观察，但对话回应、冲突过程和观点调整必须受对应缺口限制。partial 的模型分析仍需本轮显式范围与费用授权；任务包存在不等于授权。

## 9. prior_observations：既有观察，不是事实标签

每项必填：observation_id、origin_task_id、origin_type、uid、dimension、labels、assessment_status、rationale、source_comment_ids、evidence、counter_evidence、limitations、export_id、rules_sha256。

- origin_type：user_initial、thread_context 或 user_synthesis；来源是实际通过结构与证据校验的已完成任务，不收录运行中结果。
- dimension：topic_stance、discourse_function、argument_support、response_engagement、expressed_emotion、interpersonal_expression、conflict_cooperation、view_revision，对应八方向；labels 取自本轮规则，允许 []。
- assessment_status：assessable、ambiguous、insufficient、not_applicable，对应四种判断状态。
- uid 为已知 UID；observation_id 为本轮唯一字符串，origin_task_id 为真实任务 ID；source_comment_ids 为该观察实际处理过的评论集合，不能随意扩大。
- evidence 与 counter_evidence 为数组，每项 `{comment_id, quote}`；quote 是准确原文子串。正向判断不能用缺失正文的 null 冒充证据；缺失信息写入 limitations。
- rationale 为可复核理由，不索取或传递模型内部思维链；不得把模型自由文字自动当作原始引用。
- export_id、rules_sha256 必须与本任务一致。跨版本旧结果只有经过程序显式重验并产生新版本记录后才可引用，不直接携带旧结论当作当前证据。

每条引用证据均须在 comments 中提供原文，必要父链作为 context 提供。大结果的全部引用不能只指向另一个摘要而无限转述；每个判断最终落回固定原评论。最少有两份分析重复引用同一评论，不等于两份独立证据。

这里只定义“供下一阶段读取的观察投影”，不表示最终画像输出 Schema 已全部确定。后续输出适配器必须按本结构校验后才组装为输入。

## 10. 三种任务与分块规范

chunk 必填：`{group_id, index, count, estimator, estimated_input_tokens, input_token_limit, reserved_output_tokens, oversize_comment_ids}`。group_id 为同一逻辑任务的 UUID，index 从 0 开始且小于 count；count 为正整数。estimated_input_tokens 为非负整数，input_token_limit 与 reserved_output_tokens 为正整数；estimated_input_tokens 不得超过本包限额，输入限额加输出预留不得超过选定模型总窗口。超限任务保持未发布并返回原因，不制造“有效但超限”的任务包。

### user_initial / primary

按 UID 汇总本视频已取得发言，优先按楼分组。每条目标发言附根评论和可得的父链；与目标直接相关的后续回复可作为 context。prior_observations 必须为空，不先给总体标签。提取初步观察并列出需完整对话核对的问题。

同一 UID 发言过多时拆为多个包；总体 target_index 覆盖该 UID 的全部已取得发言，不能按点赞、情绪强度或长度只选部分。单 UID 在多包中的初步结果允许不同，不强制一致。

### thread_context / primary 与 reconcile

primary 输入按原始楼结构组织，prior_observations 必须为空，独立观察原文。reconcile 是后续独立任务包，依赖 primary 完成，再加入相关用户初步观察和 primary 的楼内观察。两种 phase 均属于一个逻辑分析阶段，证据只计一次。

reconcile 必须指出支持、修正或不能确认的已有判断，不能以主 Agent 的初步结论作为必须达到的答案。每个楼的 root_id 是稳定身份，000 等显示序号不能作为跨批次关联键。

### user_synthesis / primary

每包只综合一个 UID，输入包括该 UID 的初步结果、相关楼内 reconcile 结果、相反表现和变化事件。总体目标集合覆盖该 UID 全部已取得发言；相关楼任务未完成时标记 prior_result_missing，只能产生部分综合结果，不能发布最终完整画像。

结果太大时允许分组综合再合并，origin_type=user_synthesis 的中间观察必须能回溯到原评论；summary_used=true，保留覆盖集合及反例。不能多轮压缩后只剩无证据的用户标签。

### 分块与预算的强制规则

1. 预算按实际装配的规则、背景、任务包、工具声明及会话历史计算，不能只计算 comments 文件大小；具体限额由本轮执行配置给定，本规范不固定模型上下文容量。背景／规则本身已超过容量时返回 input_budget_exceeded，停止组装或调用，不能静默截断它们。
2. 优先沿回复链切分；每块保留可得根评论和必要祖先。重复的父链是 context，不计目标次数。
3. 必要上下文超过预算时登记 context_budget 与省略 ID，不能宣称已完整理解该对话。缺上下文的相关维度允许无法判断。
4. 单条评论也无法容纳时标记 oversize_comment_ids、保留待处理，禁止截断后当作整条原文。本版不定义任意片段切词拼接，进一步压缩方式须版本化补充。
5. 同一任务范围的目标清单完整保留；部分包缺失、超大目标待处理或某一包失败时，整体不能标为全量处理完成。
6. 读取工具若返回截断内容，必须完整补读并核对实际覆盖，或返回上下文不足；仅成功打开文件不算模型获得了全部内容。

## 11. response_contract：输出交接要求

必填：`{schema_ref, required_dimensions, required_identity_fields, evidence_required, allow_unknown}`。

- schema_ref：`{path, sha256}` 或 null；null 只允许在离线组装和检查样例时使用。正式模型执行前必须有已确认的对应输出 Schema，执行器不得以 null 发起自动分析。
- required_dimensions：第 9 节八个 dimension 值完整列表；要求逐项处理适用性，不要求每项都有正向结论或标签。
- required_identity_fields：固定为 task_id、run_id、video_id、export_id、target_uid、rules_sha256。
- evidence_required 与 allow_unknown 固定为 true；没有合格证据的判断不能凭模型把握高而通过。

执行器验证输出身份、原评论归属、原文证据、规则版本和覆盖范围后才接受；CLI completed/success 不是业务验收。输出可报告不适用、证据不足或部分完成，不允许用旧批次画像填补缺项。

## 12. 任务覆盖与证据校验

target_index 指向的 JSONL 每行固定为 `{comment_id, root_id, author_uid}`，身份均来自本批次，author_uid 可 null。对 user_initial/user_synthesis 逻辑任务，其集合必须等于目标 UID 的本批次全部评论；楼任务等于所选楼内目标评论集合，包括未知作者上下文，但未知作者不能形成 UID 画像。

发布前检查：任务索引身份唯一、输入摘要一致、依赖引用在当前或前序不可变清单中存在且属于本运行、DAG 无环、目标集合无遗漏或重复主归属、context 与 target 无交叉、所有 comments 均有原始记录且投影逐字段一致、观察证据有效、背景和规则摘要一致。不得让模型自行生成这些权威清单。

token 估算只控制容量，不作为已读覆盖的证据。程序记录实际交付任务与模型结果引用，不能通过“已分析全部”一句话确认覆盖；分块失败、缺规则、缺输出契约时阻止发布最终画像。

## 13. 运行版本与保存

run-manifest、任务索引、目标索引、每个 input.json、背景与规则在本轮发布后不可变，记录各自摘要。失败重试可复用完全相同任务包；重新分配时显式结束旧任务并发送当前包身份，禁止把旧范围当成永久角色权限。

对照阶段只有在依赖结果生成后才能发布对应任务包。运行清单可分阶段发布不可变版本，清单版本通过内容摘要定位；不能先伪造空结果供后续任务消费。执行器的可变状态登记与不可变输入清单分开。

完整分析运行的 inputs 依赖、任务包、上下文、观察与最终画像长期保留；来源目录或数据库状态被清理后仍应可查证。内部文件布局属于分析侧，不改变上游 v1/v2 路径协议。未经用户授权，不把原评论、画像、模型输入或会话记录提交 Git。

## 14. 验收场景

- 同 UID 跨楼、同名不同 UID：按 UID 归属，昵称不进入判定。
- 相同原评论在多包作为上下文：只统计一次目标发言和证据来源。
- 引用辱骂、反讽或回复被删：保留对象和上下文，不把引用者自动判为攻击者。
- 只有主评论的 partial 输入：缺口进入每个相关包，不产生虚构对话过程。
- primary 无初步画像；reconcile 加入后允许推翻它，不能混用两个 phase 的证据计数。
- 大楼跨块回复：重复必要祖先并登记覆盖；超大单条显式待处理。
- likes／nickname 省略而 content 逐字保留；媒体描述存在不表示已理解图片。
- 旧 export_id、规则摘要或 UID 的观察混入：拒绝发布任务包。
- 合法 JSON 中含恶意指令：作为数据处理，不能改变工具权限或目标范围。
- 当前任务包和模型实际读到的范围不一致：不能判定完成。
- schema_ref=null：离线检查可用，正式模型执行拒绝。

后续先按本规范实现合成样例、JSON Schema 和任务包组装／语义校验器，再接执行器。不因为本规范定版而自动调用模型、采集或部署。
