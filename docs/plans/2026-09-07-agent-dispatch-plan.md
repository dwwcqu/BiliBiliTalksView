# 已登记 Claude 成员的任务投递与结果闭环

日期：2026-09-07。流程沿用 brainstorming、writing-plans、subagent-driven-development 和 TDD；用户已确认继续连接输入、成员确认、结果验收及画像保存。本计划落实既有 Agent 设计，不扩展网页或真实视频批量调用。

## 目标与边界

新增 analysis_execution 的 delivery、evidence、dispatch 模块。首期一次主会话调用只投递一个已发布任务给一个已登记原成员；串行执行简化并发证据，不改变最终多成员协调目标。初始成员创建、丢失历史替代、跨任务累计预算/队列和真实模型验收不在本次范围，不伪造注册信息填补这些缺口。

主会话必须通过原生 SendMessage 恢复登记 agent_id。工具实际结果 resumedAgentId、本次 task_started 和 task_notification 的 task_id/tool_use_id 必须对应。既有本地 2.1.261 合成试验显示，恢复后的转发消息 parent_tool_use_id 可能沿用首次启动 ID，所以不以该字段单独归属本次响应。只从对应原生完成通知 summary 提取严格 JSON 响应，拒绝主 Agent 自述、Markdown 包裹、截断和旧结果。

## 投递契约（内部版本 1）

prepare_delivery(root, run_id, manifest_id, task_id, attempt_id, agent_id, rule_catalog) 从 read_state 验证实际冻结文件与历史依赖，要求输出 Schema 和登记成员可用，返回不可变 Delivery 对象：run_path、packet、member、session_id、input_sha256、message、prompt、delivery_sha256。message 是规范 JSON 字符串，包含 payload 和 payload_sha256；payload 包括协议版本、任务/attempt/目标 agent、原 packet、所有冻结资源正文、输出 Schema 及其本地引用闭包。background/评论只作数据，不作为权限或更改分析标准的指令。规则目录与固定 rules SHA 对齐。

主提示要求把 message 原样作为 SendMessage 的 message（兼容 content；同时存在必须相同）给登记 ID；明确旧任务已结束，只处理本轮 input 和规则。输出为 {delivery_sha256, task_result}，task_result 继续遵守 AnalysisOutput 1.0.0。摘要确认是接口层规则版本确认，不是模型实际理解/推理正确性的证明，仍需证据校验和人工质量评估。

同时检查主 prompt 与子 message 的真实 UTF-8 字节估计，均不能超过 packet input_token_limit，也不能超过 CLI stdin 10 MiB；不足时直接 input_budget_exceeded，不截断正文。冻结投递到 runs/<run_id>/deliveries/<task_id>/<attempt_id>/delivery.json（payload wrapper）及 prompt.txt，保存引用和摘要，不改原 packet/manifest。

## 原生事件证据

verify_delivery(raw, *, session_id, agent_id, message, delivery_sha256, expected_model, cli_version, cwd) -> dict。先 parse_events 校验整个主流与成功退出（调用方传入已验证 returncode=0的已存 stdout），再逐一验证 init CLI/version/model/cwd、唯一主 SendMessage、精确收件人与消息、工具成功返回、任务启动/完成归属及顺序。拒绝额外 Agent/SendMessage 派发、失败通知、错误 session、错误工具 ID、未确认新规则或伪造响应。只把通知中的 task_result 返回，证据记录工具/任务 ID 和 stdout SHA。

运行来源仅使用固定 2.1.261 适配器，不因版本相似而猜测兼容。原始输出从本次受管 execute 的私有调用记录读取，核对 request/outcome/prompt/stdout SHA，不接受用户上传原始日志作为可信执行授权。

## 服务接口

dispatch_task(root, run_id, manifest_id, task_id, *, request: CliRequest, agent_id, rule_catalog, execution_config, environment, authorized=False) -> dict。

request 仅提供运行策略，prompt 必须由 prepare_delivery 替换；强制 resume=True、固定登记 session、稳定 cwd（视频目录）、CLI版本/配置模型/预期实际模型/provider非空。execution_config 绑定 run、rules SHA、协议、全部登记 agent_ids、session、limits，写到 execution.json，不能在同一运行悄然更换模型。调用方提供明确预算，仍不是实际账单保证。

以运行 .dispatch.lock 覆盖准备、执行和验收；调用 execute 使用统一 <video>/sessions/cli-calls。先校验/持久化输入与执行来源，再调用；未授权不创建记录。成功后从受管 stdout 验证原生投递、保存 delivery-proof.json，签发仅内部使用的 ExecutionContext 后进入 accept_response。验收不通过返回 rejected/incomplete，不发布最终画像。user_synthesis 通过后调用 save_profile，是否最终由既有完整依赖规则决定。普通 primary/reconcile 不提前生成画像。

同一 task 已验收时先完整复核接受目录后返回已接受结果，不重发模型；不同配置仍拒绝。传输完成但验收中断时允许从同 attempt 的完整、匹配调用记录恢复验收，不重新执行 CLI。缺少完整 outcome、正文摘要不符或原生证据缺失时阻塞，不伪造授权。费用 unknown 原样保留；没有累计预算授权，不自动批量循环。

## 实施任务

- [x] delivery.py 与正反例：冻结文件/规则/Schema闭包、UID正文不漏、成员与预算、纯投递不调用模型。
- [x] evidence.py 与正反例：使用基于既有合成试验结构的全新假事件，精确恢复/原生通知与错误身份、旧规则、截断、额外派发。
- [x] dispatch.py 与合成磁盘集成：执行前无效拒绝、持久化记录、原生证据到验收、重试不重复调用、串起 primary/reconcile/synthesis 后真实 JSON 文件（内容为合成）。
- [x] 独立审阅与修复，执行新增测试、全部 backend/tests 和 Ruff。
- [x] 更新 README/设计状态并提交当前功能分支；不合并 main，不调用真实模型，不修改采集数据或认证设置。

## 技术依据与限制

[Claude 子 Agent 文档](https://code.claude.com/docs/en/sub-agents) 和本地既有2.1.261合成恢复事件确定工具关联方式。通知 summary 可能截断或是非 JSON，此时明确拒绝；不能把 completed 当业务完成。实际成员是否稳定采用新规则需后续小规模真实模型测试，当前代码只能验证投递和响应版本/证据。Windows进程清理及bare认证限制沿用适配层设计。

## 实施细化决定

- 原生结果 SHA 与投递证明一并绑定到接受记录，防止投递完成后响应被替换。旧可信内部接受接口保持兼容；dispatch_task 始终提供证明。
- 源准备状态必须 ready，partial 必须由固定准备请求显式允许；执行授权不绕过采集策略。
- 完整 label_catalog 随 payload 发送，调用前检查八方向标签目录形状；其 SHA 固定到 execution.json，同运行不能悄然更换。
- 主会话模型与子成员模型不混称。执行文件标记 reported_model_scope=main_session_only、member_models_verified=false，画像保留限制；不凭无法独立归属的转发 ID 猜测子模型。
- 原生流允许完成通知前的中间主结果及完成后的多个成功结果，最终仍须在完成后有成功结果；任一主失败或多余派发都拒绝。

- 恢复调用的主工具集合只开放 Read/SendMessage，移除 Agent 新建工具；首次启动适配层仍可包含 Agent。成员本身工具与跨成员权限仍需真实 CLI 隔离验收，不能把这一变化等同于完整安全沙箱。

- 验收器与接受目录重读也比较调用方标签目录 SHA 和原生证明绑定值；不能通过绕过调度接口改用另一套同规则文件摘要的标签。
