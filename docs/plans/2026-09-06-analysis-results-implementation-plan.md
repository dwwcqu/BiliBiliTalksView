# 输出接受与用户画像保存实施计划

日期：2026-09-06。范围已确认，依据 [AnalysisOutput 1.0.0](../references/analysis-output-protocol-v1.md)。本阶段只验证和保存本地响应，不启动模型、数据库或队列。

## 接口与职责

- analysis_results.contract：task_result、validation_receipt、user_profile/profile_candidate 的本地 Schema 校验。
- analysis_results.validation：目标分区、身份、八方向、标签目录、本人证据、前序处置校验。
- analysis_results.acceptance：由可信调用方提供 attempt、输入交付／调用授权和规则目录，发布不可变结果及回执，并加载 AcceptedCatalog。
- analysis_results.profiles：从实际已接受的最终综合及完整任务图生成 UID 文件；缺项仅保存候选。
- analysis_results.types：ExecutionContext（attempt_id、input_sha256、authorized、delivery_verified、execution_ref、agent_id）与 RuleCatalog 字典契约。凭据对象由执行器创建，不从模型输出或任意JSON文件自报字段提升得到。

## 当前实现决定

- 规则目录由调用方显式提供 rules_sha256 与八方向 labels 映射，不默认推测 Markdown 中的合法标签；缺目录或版本不符拒绝。
- 结果接受API使用现有发布运行根与 Assembly/SourceBundle，以及已经校验的 accepted 前序目录。不提供“从任意文件标为已接受”的 CLI。
- 实际输出Schema引用必须匹配已发布输入中的response_contract；离线schema_ref=null不能进入正式接受路径。合成测试构造完整输出Schema引用及交付凭据。
- result/receipt按task_id和attempt_id隔离；result先发布，receipt为接受提交点。task锁保证同一task只接受一个attempt，重复同内容可恢复，无回执文件不可消费。
- 最终文件只从已落盘并重新核验的接受目录生成，不能通过向profiles函数传入普通dict伪造接受。
- 执行来源以调用方提供且已冻结的execution_ref读取；缺少实际模型信息保留null，不在此阶段制造真实CLI身份。

## Task 1：Schema 与结果语义

- [x] 先写合法completed、partial、unable，以及未知版本、字段缺失、身份错配、空本人证据、假quote、缺方向和前序遗漏的失败测试。
- [x] 实现 validate_document(kind,value) 和 validate_result(value,packet,rule_catalog,attempt_id,input_sha256)。复用已有精确JSON codec和本地Registry，不改变输入协议。
- [x] 观察不包含origin_result，接受后才添加引用。dimension_results必须覆盖全部已知目标UID及八方向；状态优先级按规范。
- [x] 测试合法insufficient无证据可接受、未知作者无虚构UID、多subject不合并。

## Task 2：文件接受与恢复

- [x] 先写未授权／交付不完整／缺输出Schema拒绝、合法接受后重读、无回执孤儿结果不接受、同attempt重复与冲突、不同attempt已接受、磁盘篡改测试。
- [x] 结果与回执在锁内用同盘临时文件发布。已存在文件比较内容；拒绝与incomplete记录不进入目录。
- [x] load_catalog只读取有合法accepted回执且文件摘要匹配的输出，重跑语义校验，并绑定权威任务包和依赖。
- [x] 合成响应贯通已有reconcile/synthesis组装，保持JSON observations与AcceptedResult接口一致。

## Task 3：画像文件

- [x] 从完整任务图计算相关UID的全部初步、楼primary/reconcile、综合及传递依赖；对未创建的必需阶段明确保持partial，不把空required集合当完整。
- [x] 验证最高有效综合层唯一final_merge、全部目标与依赖完成、无pending/prior_result_missing后写users/<UID>.json。
- [x] 不完整时写intermediate/profiles/<UID>/<candidate_id>.json，摘要只为程序状态说明；不存在的UID拒绝。
- [x] 从固定原始记录取得昵称和标题，规则／背景／执行配置由程序填写，source coverage原样保留。未知费用不填0。
- [x] 同内容重复保存返回旧文件并保留created_at；不同内容不覆盖。测试缺依赖、旧批次、未知UID和篡改结果不能生成最终文件。

## Task 4：整体验证

- [x] 全部测试只用合成响应，不调用模型。运行新增用例、analysis_packets回归、全后端pytest和Ruff；数据库测试依旧只使用显式TEST_DATABASE_URL。
- [x] wheel包含新Schema并可离线独立导入；实际应用接口和调用前置条件写README。
- [x] 只提交源代码、合成测试和设计；真实结果与运行记录不进入Git。本分支保留待验收，不自动合并main。

模型调用、真实token/费用账本、生产执行器和画像质量人工评估仍是后续工作。布尔授权凭据是服务内可信接口契约，不是对外授权服务或签名协议。


## 实现与验收边界

已实现 analysis_results 的Schema、逐任务语义、结果接受／重载及最终／候选画像文件保存。实际接受接口按 root、run_id、manifest_id、task_id 从磁盘重建权威输入，不信调用方可变Assembly；规则目录和执行凭据仍由可信执行器提供，不接受模型自报授权。

审阅后加强为固定execution.json、实际session/agent登记、规则和attempt输入的绑定。程序回执使用允许的扩展字段execution_binding_ref关联不可变绑定摘要；它不是模型输出字段。接受目录重载逐一核对历史来源清单，零观察前序任务也不能跳过。历史任务行单独读取，画像覆盖不误将已从当前清单移除但仍被引用的前序当作不存在。

AnalysisInput Assembly 增加内部output_schemas映射，发布层校验所有非空schema_ref及字节；这是本地内部结构扩展，不改变模型输入2.0字段。默认离线null仍合法，但不能进入本结果接受路径。read_published为已验证对象重建接口，原validate命令行为不变。

输出任务模型status、程序decision、源coverage和画像analysis_coverage各自独立。partial/unable响应不进入接受目录；完整任务可保留不确定观察。source verified+gaps也保留limitations。最终同内容重试保留原created_at，不同内容不覆盖。

本阶段无真实模型调用，画像端到端测试只用明确标注的合成执行记录与响应。代码不证明调用方声明的外部授权／模型路由绝对真实，也不验证服务商账单；后续执行器必须从实际过程构造这些记录。
