# 大模型分析输入规范

- 协议标识：`BiliBiliTalksView.AnalysisInput`
- 固定版本：`2.0.0`
- 日期：2026-09-06
- 状态：本版固定模型输入任务包及其语义；离线任务包构造器、Schema 与校验器已实现，模型执行器尚未实现，不表示画像质量已经验证。
- 范围：单视频、单个固定导出批次中的用户初步分析、楼内上下文分析与用户综合分析。主 Agent 使用运行清单协调这些任务。

## 1. 效力与边界

本版取代[历史 AnalysisInput 1.0.0](analysis-input-protocol-v1.md)，新增必填综合层级、覆盖台账、观察来源结果引用和对象结构，并固定成员登记格式。属于破坏性结构修订，使用 2.0.0，不自动按 1.0.0 解释；历史文件保持不变。本次只定规范，不执行数据迁移或模型调用。

本规范与 [CommentExport 2.0.0](comment-export-protocol-v2.md)／[历史 1.0.0](comment-export-protocol-v1.md) 分离。上游原始评论和元数据不改写；任务包是由分析侧生成的只读视图。不得将模型标签混入源记录。

分析维度严格采用[已确认八方向标准](../plans/2026-09-05-discussion-analysis-standard.md)。本规范定义输入结构、覆盖语义和证据关系，不重新定义画像类别，不设置用户总分，也不替代后续最终画像输出 Schema。

字段及嵌套字段均必填；可空字段显式填 null。禁止重复 JSON 键、非有限数值、缺失必填字段或未知必填枚举；同 major 新增未知字段可以保留但不能当作指令。破坏语义的修改升 major，新增可选字段升 minor，说明勘误升 patch；固定版本不得原地改变字段含义。

除逐项另有说明外，所有 path、摘要、版本、类型标识和解释均为字符串，SHA-256 是小写 64 位十六进制；count/task_count 为非负整数而非布尔值，ID 数组元素为对应字符串类型。labels/limitations 为字符串数组，rationale/detail 为字符串；声明去重的数组不得重复。

数据必须通过输入准备模块固定后才能组装。每轮实际模型、上下文限额和费用授权由执行配置决定；生成任务包本身不授权模型调用。

## 2. 交付形式

任务包为 UTF-8 无 BOM、LF 的 JSON 文件，存放在对应完整分析运行内的 `tasks/<task_id>/input.json`。task_id 为生成的小写带连字符 UUID。不要将这些文件写入上游批次或已发布的 inputs 目录，也不要复用准备目录的 users 路径写不同模型运行结果。

主 Agent 接收 `manifests/<manifest_id>/run-manifest.json` 的路径及 SHA-256；manifest_id 为小写 UUID，每个阶段追加新清单目录，不覆盖已发布清单；子 Agent 接收明确的 task_id、任务包路径及摘要。文件引用都由程序验证，不靠 Agent 猜路径。模型可以使用 Read 读取任务包，但读取的原文会发送到所配置模型服务，并非只在本机处理。

路径引用统一为相对完整分析运行根的 POSIX 路径，禁止绝对路径、..、链接逃逸和借助文件名执行命令。原始评论来自已固定 inputs，由程序提供受控证据读取接口；模型不直接追随 current.json，不自行读取数据库或浏览媒体 URL。

## 3. 主 Agent 运行清单

每个 run-manifest.json 必填：

| 字段 | 类型与含义 |
| --- | --- |
| protocol / schema_version | 固定协议名与 2.0.0 |
| manifest_id / previous_manifest_sha256 | 本清单 UUID；上一清单摘要或 null，支持分阶段发布且不覆盖 |
| run_id / prepared_run_id | 完整分析运行 ID 与已完成准备的 prep-摘要 ID，二者不能混同 |
| video_id / export_id / export_schema_version | 视频身份、导出 UUID、实际 1.x／2.x 版本 |
| input_fingerprint | 已固定评论输入指纹 |
| resources | 第 5 节的背景与规则资源对象 |
| coverage | 第 8 节的批次覆盖对象 |
| task_index | `{path, sha256, task_count}`，引用逐行任务索引 |
| member_registry | `{path, sha256}`，引用第 16 节 JSON 成员登记快照，空登记格式亦固定 |
| group_coverage | `{path, sha256}`，引用第 15 节 JSON 逻辑任务覆盖台账，包含已发布任务和待处理目标 |
| execution_limits | `{max_active_members, max_input_tokens, reserved_output_tokens, accounting_method}`；前三项为正整数，最后为估算方法标识字符串 |

清单提供索引而非整个评论区原文。task-index.jsonl 每行必填：task_id、task_type、phase、synthesis_level、target_uid（可 null）、root_ids、input_path、input_sha256、depends_on（去重任务 ID 数组）。执行器保存权威任务状态；主 Agent 的自然语言完成声明不能替代状态登记。

成员登记只提供逻辑角色、实际 session_id／agent_id、可恢复状态及已有任务归属，不包含认证凭据。CLI 是否支持恢复需事先验证；任务包协议不保证旧 Agent 永久可用。

## 4. 子任务包公共结构

| 字段 | 类型与约束 |
| --- | --- |
| protocol / schema_version | 固定为 BiliBiliTalksView.AnalysisInput / 2.0.0 |
| task_id / run_id / prepared_run_id | 身份字符串；task_id 为 UUID，run_id 来自完整运行，prepared_run_id 关联固定准备记录 |
| video_id / export_id / export_schema_version | 与运行清单、固定输入一致 |
| task_type | user_initial、thread_context、user_synthesis |
| phase | primary 或 reconcile；仅 thread_context 可用 reconcile |
| synthesis_level | user_synthesis 为非负整数；其他 task_type 必须为 null |
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

目标唯一归属按本运行内 (task_type, phase, synthesis_level, target_uid) 分别校验，并按第 15 节在已发布目标与待处理目标间分配。同层同 UID 的逻辑任务不得重复覆盖目标；不同综合层可重新覆盖同一原评论，但不增加样本数或证据权重。复核或重试只替代原任务结果。辅助评论可以在不同任务包重复出现，其重复不增加用户发言数或证据权重。

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

每项必填：observation_id、origin_task_id、origin_type、origin_result、uid、dimension、subject、labels、assessment_status、rationale、source_comment_ids、evidence、counter_evidence、limitations、export_id、rules_sha256。subject 结构见第 17 节。

- origin_type：user_initial、thread_context 或 user_synthesis；来源是实际通过结构与证据校验的已完成任务，不收录运行中结果。
- origin_result：`{path, sha256}`，引用被冻结且被执行器接受的输出文件；文件身份必须匹配 origin_task_id、当前 run_id、export_id 和规则摘要。origin_type 必须等于来源任务的 task_type，不能仅相信观察自报来源。
- 每个 origin_task_id 必须在本任务 task-index 行的直接 depends_on 中，不能仅存在于清单而不声明依赖。depends_on 可含没有贡献观察但确有前置作用的任务；所有实际读取的模型结果都必须有直接依赖。
- dimension：topic_stance、discourse_function、argument_support、response_engagement、expressed_emotion、interpersonal_expression、conflict_cooperation、view_revision，对应八方向；labels 取自本轮规则，允许 []。
- assessment_status：assessable、ambiguous、insufficient、not_applicable，对应四种判断状态。
- uid 为已知 UID；observation_id 为本轮唯一字符串，origin_task_id 为真实任务 ID；source_comment_ids 为该观察实际处理过的评论集合，不能随意扩大。
- evidence 与 counter_evidence 为数组，每项 `{comment_id, quote}`；quote 是准确原文子串。正向判断不能用缺失正文的 null 冒充证据；缺失信息写入 limitations。
- rationale 为可复核理由，不索取或传递模型内部思维链；不得把模型自由文字自动当作原始引用。
- export_id、rules_sha256 必须与本任务一致。跨版本旧结果只有经过程序显式重验并产生新版本记录后才可引用，不直接携带旧结论当作当前证据。

每条引用证据均须在 comments 中提供原文，必要父链作为 context 提供。大结果的全部引用不能只指向另一个摘要而无限转述；每个判断最终落回固定原评论。最少有两份分析重复引用同一评论，不等于两份独立证据。

这里只定义“供下一阶段读取的观察投影”，不表示最终画像输出 Schema 已全部确定。后续输出适配器必须按本结构校验后才组装为输入。

## 10. 三种任务与分块规范

chunk 必填：`{group_id, index, count, estimator, estimated_input_tokens, input_token_limit, reserved_output_tokens}`。group_id 关联第 15 节覆盖台账，index 从 0 开始且小于 count；count 仅等于该组实际发布任务包数量，不包含待处理目标或未发布包。有效任务包的 count 为正整数；全部待处理的组不发布空任务包。estimated_input_tokens 为非负整数，input_token_limit 与 reserved_output_tokens 为正整数；estimated_input_tokens 不得超过本包限额，输入限额加输出预留不得超过选定模型总窗口。超限任务保持未发布并返回原因，不制造“有效但超限”的任务包。

### user_initial / primary

按 UID 汇总本视频已取得发言，优先按楼分组。每条目标发言附根评论和可得的父链；与目标直接相关的后续回复可作为 context。prior_observations 必须为空，不先给总体标签。提取初步观察并列出需完整对话核对的问题。

同一 UID 发言过多时拆为多个包；总体 target_index 覆盖该 UID 的全部已取得发言，不能按点赞、情绪强度或长度只选部分。单 UID 在多包中的初步结果允许不同，不强制一致。

### thread_context / primary 与 reconcile

primary 输入按原始楼结构组织，prior_observations 必须为空，独立观察原文。reconcile 是后续独立任务包，必须将覆盖其目标范围的对应 primary 任务全部列入直接 depends_on，即使 primary 返回零条观察也不能省略依赖。对应 primary 结果通过验收后，再加入相关用户初步观察和楼内观察，所有来源均列入直接依赖。两种 phase 的证据只计一次。

reconcile 必须指出支持、修正或不能确认的已有判断，不能以主 Agent 的初步结论作为必须达到的答案。每个楼的 root_id 是稳定身份，000 等显示序号不能作为跨批次关联键。

### user_synthesis / primary

所有层级的 prior_observations 中，每项 uid 必须等于 scope.target_uid，包括层 0 引入的楼内观察；不得把同楼其他用户的观察作为该 UID 的表现。其他参与者原文只能作为必要 context 提供。

每包只综合一个 UID，prior_observations 中每项 uid 必须等于 scope.target_uid；输入包括该 UID 的初步结果、相关楼内 reconcile 结果、相反表现和变化事件。总体目标集合覆盖该 UID 全部已取得发言；相关楼任务未完成时标记 prior_result_missing，只能产生部分综合结果，不能发布最终完整画像。

综合从 synthesis_level=0 开始，层 0 消费初步和楼内观察。同一层将该 UID 全部目标分配给多个包及待处理清单，所有包属于同一个覆盖组。下一层 level=L+1 只合并同 UID 的 level=L 综合结果，并把实际消费的上一层任务全部列为直接依赖，不依赖同层或更高层综合任务。原始证据可重复提供，summary_used=true，保留覆盖集合及反例。

A、B 两条评论可在层 0 分成 S1(A)、S2(B)，层 1 用 S3(A,B) 合并；唯一归属分别在层 0 和层 1 核对，不跨层累加证据。target_index 在各层始终是该 UID 的完整原发言集合，不改成局部集合以隐藏遗漏。最终合并必须是该层唯一包，目标覆盖完整集合、待处理为空，且所需上一层结果均已验收；否则只能保留部分结果，不发布最终完整画像。不允许多轮压缩后只剩无证据标签。

### 分块与预算的强制规则

1. 预算按实际装配的规则、背景、任务包、工具声明及会话历史计算，不能只计算 comments 文件大小；具体限额由本轮执行配置给定，本规范不固定模型上下文容量。背景／规则本身已超过容量时返回 input_budget_exceeded，停止组装或调用，不能静默截断它们。
2. 优先沿回复链切分；每块保留可得根评论和必要祖先。重复的父链是 context，不计目标次数。
3. 必要上下文超过预算时登记 context_budget 与省略 ID，不能宣称已完整理解该对话。缺上下文的相关维度允许无法判断。
4. 单条评论也无法容纳时，在第 15 节 pending_targets 中登记 reason=oversize_comment，不将它放入已发布包的 target_comment_ids，也不计入 chunk.count。禁止截断后当作整条原文。本版不定义任意片段切词拼接。
5. 同一任务范围的目标清单完整保留；部分包缺失、超大目标待处理或某一包失败时，整体不能标为全量处理完成。
6. 读取工具若返回截断内容，必须完整补读并核对实际覆盖，或返回上下文不足；仅成功打开文件不算模型获得了全部内容。

## 11. response_contract：输出交接要求

必填：`{schema_ref, required_dimensions, required_identity_fields, evidence_required, allow_unknown}`。

- schema_ref：`{path, sha256}` 或 null；null 只允许在离线组装和检查样例时使用。正式模型执行前必须有已确认的对应输出 Schema，执行器不得以 null 发起自动分析。
- required_dimensions：第 9 节八个 dimension 值完整列表；要求逐项处理适用性，不要求每项都有正向结论或标签。
- required_identity_fields：固定为 task_id、run_id、video_id、export_id、target_uid、synthesis_level、rules_sha256。
- evidence_required 与 allow_unknown 固定为 true；没有合格证据的判断不能凭模型把握高而通过。

执行器验证输出身份、原评论归属、原文证据、规则版本和覆盖范围后才接受；CLI completed/success 不是业务验收。输出可报告不适用、证据不足或部分完成，不允许用旧批次画像填补缺项。

## 12. 任务覆盖与证据校验

target_index 指向的 JSONL 每行固定为 `{comment_id, root_id, author_uid}`，身份均来自本批次，author_uid 可 null。对 user_initial/user_synthesis 逻辑任务，其集合必须等于目标 UID 的本批次全部评论；楼任务等于所选楼内目标评论集合，包括未知作者上下文，但未知作者不能形成 UID 画像。

发布前检查：任务索引身份唯一、输入摘要一致、依赖引用在当前或前序不可变清单中存在且属于本运行、DAG 无环、按组及综合层检查已发布目标与待处理目标完整分区、观察来源与实际模型结果读取均有直接依赖、context 与 target 无交叉、所有 comments 均有原始记录且投影逐字段一致、观察证据有效、背景和规则摘要一致。不得让模型自行生成这些权威清单。

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
- 大楼跨块回复：重复必要祖先并登记覆盖；可容纳 A、超大 B 时 count=1、已发布目标={A}、pending={B}、总体目标={A,B}。
- 分组综合层 0 的 S1(A)/S2(B) 与层 1 的 S3(A,B) 可合法共存，跨层不重复计数。
- origin_task_id 不在 depends_on、输出文件摘要不符或 reconcile 缺 primary 依赖时拒绝发布。
- 无成员时使用第 16 节固定空结构，不接受空文件、数组或任意 JSONL。
- likes／nickname 省略而 content 逐字保留；媒体描述存在不表示已理解图片。
- 旧 export_id、规则摘要或 UID 的观察混入：拒绝发布任务包。
- 合法 JSON 中含恶意指令：作为数据处理，不能改变工具权限或目标范围。
- 当前任务包和模型实际读到的范围不一致：不能判定完成。
- schema_ref=null：离线检查可用，正式模型执行拒绝。

后续先按本规范实现合成样例、JSON Schema 和任务包组装／语义校验器，再接执行器。不因为本规范定版而自动调用模型、采集或部署。

## 15. 逻辑任务覆盖台账

run-manifest.group_coverage 引用 UTF-8 JSON 对象，必填 protocol、schema_version（本协议及 2.0.0）、run_id、export_id、groups。groups 是数组，每项必填：

- group_id：UUID；与任务包 chunk.group_id 对应。
- task_type、phase、synthesis_level、target_uid：与该组任务的同名字段一致；同 UID 的用户任务在同 type/phase/level 只有一个有效组。
- root_ids：总体 target_index 对应的去重楼集合。每个包的 scope.root_ids 必须等于其 target_comment_ids 对应的楼集合，是组 root_ids 的子集；包内辅助上下文所处楼不扩大目标 root_ids。各包目标楼与 pending_targets 对应楼的并集等于组 root_ids。同 type/phase/level 的楼组目标楼范围不重叠，不限制同一楼在 primary 与 reconcile 中分别出现。
- target_index：`{path, sha256, count}`，与所有该组任务 scope.target_index 一致；用户任务在每层都覆盖该 UID 全部原发言。
- published_tasks：数组，每项 `{task_id, index, target_comment_ids}`。task_id 在当前有效 task_index 中，index 连续为 0..N-1；每项目标非空，必须逐项匹配实际包。N 可以为 0，此时该组不发布任何任务包。
- pending_targets：数组，每项 `{comment_id, reason, detail}`。reason 为 oversize_comment、input_budget_exceeded 或 dependency_missing；detail 为非空说明。comment_id 必须在总体 target_index，且每个待处理 ID 唯一。
- final_merge_task_id：UUID 或 null。非 user_synthesis 固定 null；综合组仅在 N=1、待处理为空、唯一包目标等于完整集合时可指向该包。其他情况 null。该字段仅表示最终合并候选，输出仍需验收，不能据此宣称完成。

设总体目标集合为 U，已发布各包目标为 T_i，待处理集合为 P，强制满足 U = P ∪ T_0 ∪ ... ∪ T_(N-1)，且所有集合两两不交。每个有效包 chunk.count=N。已发布但执行失败的包仍属于 T_i，失败登记在执行状态中，不临时把其目标移到 P。

待处理信息属于逻辑组，不塞进其他包的“超大目标”字段。没有可发布包时，主 Agent 仍能通过该台账看到目标及阻塞原因。整个组是否完成以所有目标任务结果通过、P 为空为条件。

台账随阶段清单不可变发布。若改变分块或解决待处理目标，发布新清单及完整的新组定义，并重新生成需要变更 index/count/scope 的任务包；不混用旧块计数和新台账。新清单必须完整列出本次有效任务和组，旧清单保留历史但不参与本次目标计数。直接依赖可以引用前序清单中的已接受结果；结果复用须重新验证身份与输入等价，不把新包自动标为已完成。

## 16. 成员登记快照

member_registry 引用 UTF-8 JSON 对象，固定必填：protocol、schema_version（本协议及 2.0.0）、registry_id（UUID）、run_id、video_id、captured_at（UTC 秒级 Z 时间）、main、members。登记是供本轮读取的状态快照，不能作为绕过实际会话存活和恢复检查的依据。

main 必填对象：`{session_id, status}`。session_id 为 Claude 主会话 UUID 或 null；status 为 uncreated、available、running、unavailable。uncreated 必须为 null，其他状态必须有实际已登记 ID。available 只表示有已保存会话记录，不能保证恢复一定成功。

members 为数组，每项必填：

| 字段 | 类型／约束 |
| --- | --- |
| member_id | 运行程序登记的逻辑成员字符串，满足 ^[a-z0-9_-]+$，本视频内唯一 |
| agent_id | CLI 实际返回的非空字符串，不要求是 UUID，不伪造 |
| parent_session_id | UUID，必须等于 main.session_id |
| role | user_initial、thread_context 或 user_synthesis；跨阶段换角色必须重新验证配置兼容性 |
| status | available、running、completed、failed、unavailable |
| active_task_id | UUID 或 null；running 必须对应当前或前序已发布任务，其他状态为 null |
| task_ids | 本 run_id 内曾正式分配的去重任务 UUID 数组，须能在本轮当前或前序清单定位；active_task_id 非空时必须属于它。旧运行的历史归属保留在执行器持久登记，不混入本快照 |
| role_sha256 | 已登记角色配置的字节摘要，不等同“恢复后一定已加载新配置” |

同一 agent_id 只能登记一次，不能将不同逻辑名字当作不同实例计数。main 未创建时 members 必须为空；成员为空时不能虚构角色实例。首次空登记如下（ID 为示意值）：

```json
{
  "protocol": "BiliBiliTalksView.AnalysisInput",
  "schema_version": "2.0.0",
  "registry_id": "11111111-1111-4111-8111-111111111111",
  "run_id": "22222222-2222-4222-8222-222222222222",
  "video_id": "bilibili:video:10001",
  "captured_at": "2026-09-06T00:00:00Z",
  "main": {"session_id": null, "status": "uncreated"},
  "members": []
}
```

同视频另一个运行仍占用主会话时，执行器先等待或返回 busy，不能在新运行登记中伪装其活动任务属于本轮。登记更新时发布新 registry_id 文件和引用它的新清单，不能覆盖被旧清单引用的文件。持久会话登记的可变工作记录由执行器维护，不写入这个不可变快照；成员登记不会赋予超出本轮授权的工具权限。

## 17. 观察的对象／命题

prior_observations.subject 为必填对象：`{kind, label, proposition, target_uid, evidence_comment_ids}`。

- kind：topic、proposition、participant、group、content 或 unknown。
- label：非空对象描述；kind=unknown 时为 null。
- proposition：具体命题字符串或 null；kind=proposition 时必须非空，其他类型可为 null。
- target_uid：正整数字符串或 null；仅 participant 可非空，且必须有本批次明确的对象关联证据，不能通过昵称猜测。
- evidence_comment_ids：去重评论 ID 数组，必须出现在本包 comments 中；用于定位对象或命题，而不是额外独立证据权重。

话题立场、情绪表达、观点调整必须给出可定位对象，无法确定时使用 unknown 并将对应判断标为 ambiguous 或 insufficient。其他维度也必须显式给出对象，确实无法归属时可用 unknown，不以解释文字暗藏确定性结论。

综合时不能仅按相同 labels 合并不同 subject。例如对“配乐”的支持与对“结局”的反对分别保留；承认事实记错不自动变为总体立场变化。subject 描述来自可复核证据，不用于推断被提及者的真实身份或群体归属。论述依据的进一步子项结构随后续输出契约细化，本次不新增分析维度。

## 18. 依赖及跨层验收

执行器在发布包前强制检查：每项观察 origin_task_id 在本任务直接 depends_on 中；origin_result 文件摘要、输出身份、任务类型及已接受状态匹配；不得只凭观察自报来源通过。来源位于前序清单时仍要读取该来源版本，不自动改接新的替代结果。

reconcile 依赖覆盖本包目标的所有 primary 任务，即使对应输出没有观察；如果 primary 缺失／失败，其相关目标进入 dependency_missing 或等待，不凭空构造对照输入。层 L>0 的综合观察只能来自同 UID 的 L-1 层，不能形成同层互依赖。任何额外读取的模型结果也必须登记直接依赖。

当生成更高层综合包时，若上一层尚未覆盖部分目标，这些目标进入新层 pending_targets；已发布包只综合依赖齐全的目标子集。最终候选需待处理为空且覆盖完整原集合。跨层及跨 phase 的重复原文仍按 comment_id 去重，不把推理层数或 Agent 数量当作证据数量。
