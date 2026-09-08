# 大模型分析输出与用户画像文件规范

- 协议标识：`BiliBiliTalksView.AnalysisOutput`
- 固定版本：`1.0.0`
- 日期：2026-09-06
- 状态：本版固定任务响应、结果验收及最终 UID 文件结构；对应 Schema、本地结果接受器和用户文件保存已实现，模型执行器尚未实现；真实授权和交付事实由可信执行器提供。
- 输入依据：[AnalysisInput 2.0.0](analysis-input-protocol-v2.md)。

## 1. 范围与责任

本规范沿用[八方向分析标准](../plans/2026-09-05-discussion-analysis-standard.md)，不新增画像类别、不设置跨方向总分，不推断永久人格或跨视频全局身份。

模型返回任务 JSON；运行程序验证结构、身份、证据、覆盖与依赖后，冻结已接受结果。最终用户画像由程序根据合格的综合任务结果和固定来源数据写入文件，不让模型直接写 users 目录、选择路径、伪造接受状态、生成会话 ID 或填写自报费用。

字段及嵌套字段均必填，可空值显式 null。JSON 使用 UTF-8 无 BOM、LF，拒绝重复键、NaN／Infinity，保留数字精度。数字外部身份为正整数字符串；UUID 为小写带连字符；SHA-256 为小写64位十六进制；时间为有效 UTC 秒级 Z 格式；整数不接受布尔值。未知必填枚举拒绝，同 major 未识别字段可以保留为数据，不扩展权限或状态。

字段语义破坏性修改升 major；固定版本不原地改含义。本输出版本与输入、导出、分析规则版本分别管理。

## 2. 三种对象及其保存位置

| 对象 | 产生者 | 位置与作用 |
| --- | --- | --- |
| 任务响应 TaskResult | 模型 | 单个 JSON 对象，不包含 Markdown 围栏或额外说明；由程序记录到本次 attempt 的临时位置 |
| 已接受任务结果 AcceptedTaskResult | 程序 | results/<task_id>/<attempt_id>.json，包含规范任务响应原内容；同 task_id 只选择一个接受版本 |
| 最终用户画像 UserProfile | 程序 | users/<UID>.json，按完整分析运行隔离，不能写入 prep-摘要运行 |

所有路径相对完整分析运行根。程序从已校验 UID 生成文件名，不信任模型提供的文件名。原始评论位于分析侧固定 inputs，通过 video_id、export_id、input_fingerprint 绑定，不使用含 .. 的相对路径穿出运行目录。

原始响应只保存本任务最终输出文本，不索取或落盘内部思维链、完整 HTTP 响应或凭据。解析失败文本可在 intermediate/tasks/<task_id>/<attempt_id>/response.txt 中留作受控检查；不作为成功结果，不进入 Git。

## 3. TaskResult：模型必须返回的顶层字段

| 字段 | 类型与约束 |
| --- | --- |
| protocol / schema_version | BiliBiliTalksView.AnalysisOutput / 1.0.0 |
| artifact_type | 固定 task_result |
| task_id / run_id | 与输入包一致，run_id 为完整分析运行，不是 prepared_run_id |
| attempt_id | 程序在本次调用前提供的 UUID，模型只回显 |
| video_id / export_id | 与输入及固定原文一致 |
| task_type / phase / synthesis_level | 与输入包一致；非综合 level=null，综合为非负整数 |
| target_uid | 与 scope.target_uid 一致；楼任务为 null |
| input_sha256 / rules_sha256 | 原输入文件字节摘要与本轮分析规则摘要，由程序提供，模型回显 |
| model_status | completed、partial 或 unable；这是模型声明，不是程序接受状态 |
| coverage | 第4节的任务内处理范围 |
| observations | 第5节观察数组，没有时 [] |
| dimension_results | 第6节逐 UID、逐方向的覆盖说明 |
| prior_dispositions | 第7节对输入既有观察的处理说明 |
| summary | 对本任务结果的简短文字说明；不得新增无观察证据的事实判断 |
| limitations | 字符串数组，保留缺口及不能判断的情况 |

不要求模型生成模型型号、真实时间、成本、完整进程状态或磁盘保存状态。它们由执行器单独记录，不能从模型文字推定。

completed 仅表示本包目标均已处理，允许观察为 insufficient 或 not_applicable，也允许原始采集为 partial；不代表整个视频数据完整或结论确定。partial 表示仍有本包目标未处理，unable 表示本包目标均未处理。响应与实际分区不符时拒绝，不自动修正 model_status。

## 4. coverage：本包目标分区

coverage 固定为 `{processed_comment_ids, unprocessed_targets, limitations}`。

- processed_comment_ids：去重评论 ID 数组，仅能来自本包 scope.target_comment_ids。
- unprocessed_targets：数组，每项 `{comment_id, reason, detail}`；reason 为 context_unavailable、capacity_exceeded、dependency_unavailable 或 unable_to_process，detail 为非空说明。
- 两个集合必须互不相交，其并集必须等于本包目标集合；每个未处理 ID 只能出现一次。辅助 context 不得被充当额外已处理目标。
- completed 要求未处理数组为空；partial 要求两个集合均非空；unable 要求 processed 为空且未处理集合等于全部目标。当前发布的任务包目标非空，空任务响应不能冒充完成。
- 阅读了发言但无法判断其含义可列为 processed，并在对应方向标为 insufficient；确实没有处理该目标时才列为 unprocessed，不能将两者互换。

模型报告的 processed 是待核验声明，不是阅读证明。程序还要核对实际交付的输入版本、上下文截断、缺失的工具读取结果及前序依赖。无法证明本轮输入完整交付时不得接受 completed。

源采集 coverage 不由模型重填，最终文件由程序从固定 manifest 原样复制；模型只能在 limitations 解释其影响，不能将 partial 改为 verified。

## 5. observations：有对象、有证据的观察

输出观察采用 AnalysisInput 2.0 的观察投影字段，唯一例外是**不包含 origin_result**，避免结果文件引用自身 SHA-256 的循环。输入组装器在接受后添加 `{path, sha256}`。

每项必填：observation_id、origin_task_id、origin_type、uid、dimension、subject、labels、assessment_status、rationale、source_comment_ids、evidence、counter_evidence、limitations、export_id、rules_sha256。

- observation_id 格式为 `<task_id>:o<正整数>`，同一已接受任务内唯一；不能沿用前序 observation_id 冒充新观察。
- origin_task_id 等于当前 task_id，origin_type 等于当前 task_type；uid 为本包目标发言涉及的已知 UID。用户任务只能输出 target_uid；未知作者集合不能生成观察主体 UID。
- dimension 固定为 topic_stance、discourse_function、argument_support、response_engagement、expressed_emotion、interpersonal_expression、conflict_cooperation、view_revision。
- labels 为本轮规则允许的字符串数组，可为空，不临时新增正式类别。自动接受前，程序须将已确认规则的合法取值／子项形成与 rules_sha256 绑定的可校验目录；目录缺失或版本不匹配时阻止自动接受，不能凭任意字符串认定有效。该目录只能表达已确认标准，不能顺便自行选定新标签。
- assessment_status 为 assessable、ambiguous、insufficient 或 not_applicable。
- subject 固定为输入规范第17节对象，所有字段保留；不得仅把对象藏在 rationale 中。明确不同对象的观察不能因为 labels 相同就合并。
- source_comment_ids 非空且去重，必须是本任务 processed_comment_ids 的子集，并至少含一条该 uid 的目标发言；其他人的发言只能解释上下文，不作为此 UID 自己发表的内容。
- evidence 和 counter_evidence 每项为 `{comment_id, quote}`。comment_id 必须出现在本包实际交付 comments 中，quote 为非空、准确原文子串；正文为 null 时不能作为引用。
- assessable 必须有至少一条来自该 uid 本人目标发言的支持 evidence；其他作者的引用可解释上下文或提供反例，不能单独用来证明此 UID 的行为。证据不足时不通过伪造、空引用或改写原文满足数量要求。insufficient/not_applicable 可以无证据，但必须说明限制或不适用原因。
- rationale 为可复核解释，不包含或索取内部思维链。模型对原文的解释不等于原文引用。
- export_id、rules_sha256 与顶层一致。

对 participant 的 subject.target_uid 必须有输入原文／来源关系支持，不通过昵称猜测。对 topic_stance、expressed_emotion、view_revision 无法定位对象时，使用 unknown 且状态为 ambiguous 或 insufficient。

多条证据支持某次判断不等于该用户的永久性质；出现攻击与出现礼貌表达可同时保留。未观察到某行为不等于证明没有该行为。

## 6. dimension_results：八方向均有交代

dimension_results 为数组，每项 `{uid, dimension, assessment_status, observation_ids, limitations}`。

对于本任务目标集合中的每个已知 UID，八个方向各有且仅有一项；unknown 作者不构造虚拟 UID。即使某 UID 的目标暂未处理，也必须用 insufficient 及明确限制说明，不能默默省略该用户。完全只有未知作者的楼任务允许此数组为空，同时保留任务 coverage。

observation_ids 是当前响应 observations 的去重引用；每项被引用观察必须属于相同 uid、dimension。每条输出观察必须且只能归入对应方向一项，不能成为游离结论。

方向状态按当前引用观察归纳：有 assessable 则为 assessable；否则有 ambiguous 则为 ambiguous；否则有 insufficient 则为 insufficient；其余为 not_applicable。这只是摘要优先级，不覆盖各条观察的差异。

无观察时仅允许 insufficient 或 not_applicable，并要求非空 limitations；若该 UID 还有未处理目标且此方向无观察，只允许 insufficient，不能用 not_applicable 掩盖未处理。已有观察按上述优先级归纳，同时在 limitations 保留未处理范围。每个方向不要求都有标签；不得为填满八项强行判断。

## 7. prior_dispositions：对已有判断的处理

每项必填 `{prior_observation_id, origin_task_id, decision, replacement_observation_ids, rationale, evidence}`。

- prior_observation_id、origin_task_id 必须对应本输入包实际包含的某一观察，不引用未分配的外部结果。
- decision 为 confirmed、revised、rejected、unresolved；它们是任务协作状态，不是新画像标签。
- replacement_observation_ids 引用本响应观察，且主体 UID 对应；confirmed/revised 必须非空。rejected 可为空，unresolved 可为空或指向不确定观察。
- rationale 必填非空；evidence 为准确原文引用数组，格式与第5节相同。拒绝或修正已有判断必须有证据或明确缺失上下文的解释，不能仅写“主 Agent 要求如此”。
- 输入 prior_observations 为空时本数组必须为空；否则每条输入观察恰有一项处置，不能丢掉不符合主要结论的反例。

primary 独立阅读不含初步画像，因此不得输出伪造的前序处置。reconcile 可以支持、修正、拒绝或暂无法确认初步观察。综合阶段必须保留对象差异和反例；对跨块只处理一部分的既有观察，明确限定本包范围，不把局部确认写成全局确认。

## 8. 程序验收与 AcceptedTaskResult

程序按顺序检查：

1. 本轮确实已获调用授权，绑定当前 attempt、输入文件摘要、实际规则与模型执行配置；不存在未经登记的替代 Agent。
2. 最终输出能解析为单个规范 JSON 对象；身份、阶段、层级、UID、版本全部匹配。
3. 本包目标完整分区；八方向覆盖与观察引用一致；原文、对象和证据匹配固定输入，labels 符合规则。
4. 输入清单依赖、前序接受状态、source result文件和摘要仍有效；模型没有用旧批次或缺失输入冒充本轮。
5. 本轮输入交付和读取没有未解决截断或缺失；模型 completed 声明与实际情况相符。

本版只有通过上述检查且 model_status=completed 的响应能进入 AcceptedCatalog。partial/unable 即便结构合法，也只保留为中间结果，不标记 accepted，不供后续任务当成完整前序结果。source partial 与本包 completed 是独立维度，不因采集 partial 自动拒绝一个已获部分分析授权的任务结果。

验收记录由程序生成 JSON，必填 protocol、schema_version、artifact_type=validation_receipt、task_id、run_id、attempt_id、input_sha256、decision（accepted／rejected／incomplete）、reason_codes、validated_at、result_ref（`{path,sha256}`或null）。reason_codes 为稳定程序码数组，不能直接写原始异常或凭据。Schema失败、身份错误、证据错误、未完整处理等不能用模型自报成功覆盖。

通过验收后先将任务响应写入临时文件并关闭，计算 SHA-256；以不可覆盖方式发布 results/<task_id>/<attempt_id>.json，再提交 accepted 验收记录。两步不承诺跨文件事务：只有验收记录和结果引用均有效时可消费；崩溃留下的无回执结果不算已接受。重试核对已有同 task/attempt 文件，内容相同可完成未完成提交，内容不同返回冲突，不覆盖。

每个 task_id 只选择一个 accepted attempt。同一 task/attempt 重复提交相同内容返回原接受引用；task 已接受后再提交不同 attempt 返回 already_accepted，不因其语义相似而建立第二个接受版本；需要修订已经被后序使用的结果时创建新的任务版本与依赖关系，不原地替换。无效与未完成尝试仍留在自己的 attempt 目录，不能混入 AcceptedCatalog。

生产接受器创建现有 AcceptedResult：packet 取权威输入包、result_path/bytes 取已冻结文件、observations 取已接受响应原数组，status 由程序设为 accepted。后续输入组装器添加 origin_result 引用，不要求输出文件携带自身 hash。当前只存在离线合成目录接口，不能将其当成生产接受器已经实现。

## 9. UserProfile：最终可存储的用户画像

最终路径固定为 `data/analysis/bilibili-video-<aid>/runs/<run_id>/users/<UID>.json`。顶层必填：

| 字段 | 类型与来源 |
| --- | --- |
| protocol / schema_version / artifact_type | 本输出协议、1.0.0、user_profile，由程序填写 |
| video_id / uid / display_nickname / video_title | 固定输入来源；UID必填，昵称和标题可null，不能让模型编造 |
| run_id / prepared_run_id / export_id / export_schema_version | 权威运行及来源身份，由程序填写 |
| input_fingerprint | 固定输入精确摘要，由程序填写 |
| created_at | 实际保存时间，由程序填写 |
| context | 第10节上下文与执行来源 |
| source_coverage / corpus_counts | 固定 manifest 原样复制，不能改成用户行为指标 |
| analysis_coverage | 第11节该 UID 的分析覆盖 |
| summary | 最终 user_profile 来自被接受的最终综合响应；profile_candidate 仅用程序生成的未完成状态说明，不伪造最终综合结论 |
| dimensions | 按八方向固定顺序的数组，结构见下文 |
| source_results | 去重引用数组，每项 `{task_id,attempt_id,path,sha256}`，关联被接受结果 |
| limitations | 源缺口、分析限制及不确定性说明，不因保存成功而删除 |

每项 dimensions 固定为 `{dimension, assessment_status, observations, limitations}`；八项不得缺失或重复。observations 为第5节观察结构，加上程序填写的 origin_result；该字段引用实际已接受结果，不指向画像文件自身。多个 subject 可以在同一方向并存，状态按第6节规则归纳。

顶层 uid 与所有观察主体一致。八方向顺序固定为 topic_stance、discourse_function、argument_support、response_engagement、expressed_emotion、interpersonal_expression、conflict_cooperation、view_revision。

画像文件保留原文证据片段，便于单独阅读；完整上下文仍通过固定 inputs 查证。单独复制此 JSON 不代表已备份所有原评论与来源结果，完整迁移需一起保存依赖文件。

## 10. context：画像所依据的背景、规则和模型

context 固定对象：background、analysis_rules、role_prompt、coordination、execution_ref。

前四项均为 `{path,sha256,version}`，path 关联完整运行中冻结文件。background 的 version 为程序登记的背景版本字符串，可直接使用其SHA-256；不得以文件修改时间替代内容版本。其余版本取本轮明确配置，不能由模型猜测。

execution_ref 为 `{path,sha256}`，指向程序冻结的执行配置／执行记录，至少包含实际 CLI 版本、configured_model、reported_model（可null）、provider、主 session_id（可null）、实际参与 agent_ids、输入／输出协议版本和运行限制。reported_model 缺失时写 null，不从配置别名推断服务实际路由。

模型成本和token使用记录在运行执行记录中，缺失写 null，注明统计范围和来源；不把整轮共享费用平均成每个用户的实际费用，不将未知记成0。画像仅引用它，不要求模型输出或自行估计费用。

## 11. 最终画像的覆盖与发布条件

analysis_coverage 固定为 `{status, target_comment_ids, processed_comment_ids, unprocessed_targets, required_task_ids, accepted_task_ids, missing_task_ids}`。

- target_comment_ids 是该 UID 在本 export_id 中全部已取得评论；程序从固定输入计算，不从模型报告推断。
- processed_comment_ids 是已接受任务在目标范围内的去重覆盖；不得把同一原文在多个阶段／层重复读取算成更多评论。
- unprocessed_targets 每项 `{comment_id,reason,detail}`，目标未被完整处理的原因；格式同第4节。
- required_task_ids 根据目标 UID、相关楼、综合层和实际依赖计算；accepted_task_ids 与 missing_task_ids 为完整互斥分区，不能漏掉返回零观察的必需 primary 任务。
- status 为 complete 或 partial。complete 要求目标全部已处理、unprocessed为空、missing为空，且最终综合候选已接受；否则为partial。

users/<UID>.json 只发布 analysis_coverage.status=complete 的画像。partial 的候选保存到 intermediate/profiles/<UID>/<candidate_id>.json，candidate_id 为程序生成UUID，保留进度而不冒充最终文件。它采用同样 UserProfile结构，但 artifact_type=profile_candidate，不能成为最新有效最终画像。

“分析覆盖 complete”表示处理完这批已取得的输入，不表示源站全量。若源采集为 partial，即使当前UID的分析已完成，source_coverage 仍为partial，limitations必须保留，展示不能称为完整评论区画像。verified+gaps同样保留缺口。

最终来源必须是同 UID 最高有效综合层中覆盖全目标的唯一 final_merge_task_id；其完整依赖、前序结果及 group_coverage.pending_targets 都要通过检查。prior_result_missing、未解决的超大目标或尚未接受的必需任务不得被高层摘要掩盖。对所有方向均判为insufficient的已完成分析可以保存，但不得改写成有把握的画像。

最终 user_profile.summary 必须来自该被接受最终综合结果。profile_candidate 可汇集已接受的阶段观察；尚无观察的方向按第6节记 insufficient 并说明缺项，source_results 可为空。候选 summary 只说明处理状态，不由程序重新推理用户表现。若需要改写最终摘要，应视为新任务并重新验收。

## 12. 文件保存、复用与长期保留

所有模型结果和画像都由程序在校验通过后写入。先写同盘临时文件、关闭并核对字节，再原子发布目标路径；同 run_id/UID 已有文件时，验证其结构、来源、上下文、覆盖和观察；期望内容除 created_at 外全部相同时返回原文件并保留首次 created_at，不能因重试时间变化制造冲突或重写时间。其他内容不同时报告 profile_conflict，不覆盖历史。运行中模型无权限直接修改已接受 results、inputs 或最终 users 文件。

同视频新评论、README、规则或模型配置变化时，使用新的完整分析 run_id，复用会话身份但不覆盖旧画像。prep-摘要目录不能存放完整模型结果，prepared_run_id 仅作来源关联。

未来最新画像索引只有在最终文件有效发布后才更新，不以目录时间或模型自报成功作为依据；索引更新失败不破坏已保存文件，恢复时核对后重建。首期保存功能不依赖最新索引存在。

已接受响应、验收记录、最终画像、固定输入、背景规则和执行来源长期保留在分析侧；不依赖会被清理的上游目录、数据库状态或CLI历史。备份和恢复需要覆盖这些依赖，不将“文件存在”当作备份完成。真实评论和画像不进入Git。

## 13. 正反例验收要求

| 正例 | 对应反例必须拒绝或保留为未完成 |
| --- | --- |
| 原文准确引用且uid匹配 | 伪造quote、引用他人发言冒充目标用户、旧export_id |
| assessable观察有支持证据 | 有标签但evidence为空或正文null |
| insufficient、无标签、无证据并说明限制 | 为填满八项伪造确定判断 |
| 每个已知目标UID八方向齐全 | 缺方向、重复方向、遗漏某UID、引用游离观察 |
| processed与unprocessed完整分区 | 重复目标、加入辅助发言凑覆盖、漏未处理目标 |
| 初步判断在reconcile中被有据修正 | 未提供输入的prior ID、丢弃反例不说明、只凭主Agent要求改变结论 |
| 多subject分别保存 | 同标签便合并不同命题或对象 |
| source partial但本包处理完整 | 将任务completed改写为源采集verified |
| 原结果发布后有有效接受回执 | 只有结果文件或模型自报accepted便进入接受目录 |
| 同UID完整最终综合与依赖齐全 | 缺前序任务却输出最终users文件、用旧运行结果填缺项 |
| 同run/UID重复保存相同文件 | 不同内容静默覆盖历史 |

正反例应先验证合法基线，再只修改目标条件并重新计算必要摘要，避免反例仅因旧hash而失败。此类工程测试验证协议与证据约束，不证明模型画像判定准确；模型质量仍需人工标注样例和另行授权的小批真实评估。

## 14. 后续实施边界

本输出规范的 JSON Schema、任务响应校验、验收回执、AcceptedCatalog 适配和最终用户文件发布已实现并以合成响应验证。下一阶段接入 Claude CLI 主／子 Agent 执行及真实成本、重试管理。

本版未改变 AnalysisInput 2.0.0：其 response_contract.schema_ref 可指向后续实现的 task_result Schema；其 prior_observations 由已接受输出加上 origin_result 投影得到。原离线fixture中的最小结果信封不是生产输出协议，执行接入时必须使用本规范并经接受器验证。
