# 主会话与子成员初始化、持久登记及联调

日期：2026-09-07。沿用已确认的 brainstorming / writing-plans / subagent-driven-development / TDD 流程。用户确认继续初始化登记与小规模 CLI 联调；用户已明确本轮真实验证累计 CLI 估算配额不超过 1 美元，使用合成内容和用户指定 CLI。兼容服务账单不由 CLI 估算保证。

## 范围

每视频一个持久 team。调用方显式传入 member_id/role 清单，不固化生产成员数量。每次主调用只创建一个自定义子成员，逐个完成原生握手后再创建下一个。初始化后把真实ID绑定到新不可变任务清单，并连接已完成的 dispatch_task。未完成或失败创建保留记录，绝不通过换 attempt 静默重新创建。

不扩展网页、采集、生产部署。CLI 历史存放于视频 sessions/claude-state，通过显式 CLAUDE_CONFIG_DIR 配置，并在后续 dispatch 使用同一目录。ID/证明持久保存不等于永久可恢复；CLI历史清理与备份仍需独立策略，本阶段不承诺绕过官方保留期限。

## 接口

- CliRequest 增加 agents: dict|None 默认 None。初始化每步传入一个自定义类型，固定 tools=[Read]、model=inherit；只有提供显式 definitions 的初始化恢复调用允许主 Agent 工具，普通恢复保持 Read/SendMessage。--agents 参数 JSON 与摘要都进入调用记录；校验 Windows 命令长度，不截断提示。dispatch_task 拒绝带 agents 的请求。
- verify_creation(raw, *, session_id, member_id, role, native_type, message, initialization_sha256, expected_model, cli_version, cwd) -> dict(agent_id, creation_tool_use_id, stdout_sha256)。固定2.1.261，验证init，唯一原生Agent调用的类型及完整prompt，task_started、成功tool_result、完成通知和握手JSON {initialization_sha256,member_id,role}；任一额外创建/派发、错误ID/角色、重复/不完整事件拒绝。原生task_started.task_id是实际agent_id，不取模型自述ID。
- initialize_team(root, run_id, manifest_id, *, request, members, rule_catalog, provider, expected_model, environment, authorized=False) -> dict。准备必须ready且partial有固定允许策略。request.cwd=视频根；首次使用request.session_id，request.attempt_id作为初始化ID。members为[{member_id,role}]且唯一，role取既定三种；类型名采用member_id并限制小写字母数字连字符。冻结角色/规则/背景/协调正文，不分配任何真实评论分析。
- load_team(video:Path) -> dict，验证完整团队与创建证明。initialize_team返回团队字段加status/reused；已完成团队同视频、相同成员与模型/角色配置时返回已有ID，不新增CLI调用；未完成意图存在时只能继续完全匹配的原计划。
- bind_team(root,run_id,manifest_id,rule_catalog) -> publication dict，把load_team验证后的成员绑定到新的registry与manifest；原清单不修改。按role将任务确定性轮转分配，当前任务缺少角色时拒绝。再次绑定已经一致的清单可复用。

## 持久化与恢复

video/sessions/team/intent.json 在任何模型调用前不可变发布；保存初始化ID、主session、环境状态目录、配置、完整步骤请求与分配预算。每步attempt=uuid5(initialization_id,member_id)，预算按总配额均分并向下保留6位小数；合计不超过总配额，不回收已发起步骤的预留额，即使费用未知也不计零。首次步骤resume=False，后续resume=True且显式agents定义，只创建对应成员。每步使用现有 sessions/cli-calls 记录。

每个 members/<member_id>/proof.json 绑定本步 request/outcome/stdout 副本及握手内容、真实ID、工具ID。team.json 最后发布，引用intent与所有成员证明，保存video_id/session_id/cwd/cli_state_dir/configured_model/reported_model/provider/cli_version、成员映射与initialized_at。load_team必须重新校验引用摘要与原生证据。证明文件路径按团队根相对定位，禁止外部路径。

同一次完整CLI响应可重做本地登记而不重发模型；缺request/outcome或失败/无法确认创建的响应立即阻塞，保留原主会话。不同模型/成员清单/角色配置不能悄然改写已存在团队；新增成员或角色系统提示迁移需单独流程。当前规则和背景更新由后续任务投递明确传递，不能声称旧成员系统提示已被自动改写。

## 实施任务

- [x] 原生创建证据解析及正反例（先RED再实现）。
- [x] 初始化命令definitions、定义摘要、运行记录及工具边界测试。
- [x] 团队初始化控制器、持久证明、崩溃/重复/费用预留测试。
- [x] 新registry/manifest绑定及后续dispatch状态目录对齐测试。
- [x] 独立审阅、完整后端回归及Ruff。
- [x] 执行受管合成真实CLI验证，报告真实限制，更新文档并提交功能分支。
- [ ] 整组真实初始化及跨进程子成员复用验收：因取消成员被CLI拒绝恢复而未完成，等待执行策略选择。

## 真实验证门槛

读取用户指定 C:\Users\dengww\.local\bin\claude.exe，版本已再次核对为2.1.261。当前用户设置模型deepseek-v4-pro、服务api.deepseek.com；进程环境有另一服务主机，联调显式使用用户settings中的服务/认证配置，不继承冲突路由，也不输出令牌或改写全局设置。仅AUTH_TOKEN存在，bare模式兼容性需要先核验；不擅自把token改作API_KEY。失败不自动改账号/服务/模型。所有试验材料和CLI历史只放Git忽略目录。

## 实施修正与未完成验收

- 原生流程切换为显式非bare管理模式，禁用隐式settings/hooks/MCP；bare默认仍可用于普通无代理传输。create_agent仅控制创建权限，agents可在恢复时加载原始定义。
- JSON容器排版允许差异，但字段、字符串原文、数组顺序和摘要保持一致。SDK兼容展示字段可截断；拒绝诊断以规范message及关联原生工具结果为准，不放宽成功交付。
- 空主恢复只针对完整零活动证据，同ID重试必须完整配置相同；恢复历史不可变并重验。原始已发生费用单独保留。
- 子成员身份完整且握手匹配时，可在父进程仅因budget结束的情况下保留身份，标记budget stop；不放宽AnalysisOutput验收。费用按原始和修复调用逐一计入，未知/冲突值阻塞。仅工具调用前的启动零费用可忽略，后续正费用必须一致。
- spend_limit为单独显式准入额度；超额后至少保留两倍最大已知调用费用及全部剩余预留。它不是硬账单上限。
- 已创建但未握手的成员只能经原生出生证明后，向原ID做受限修复；不能生成替代ID。修复也需完整原生交付和原始握手匹配，支持本地发布中断重建。
- 当前CLI已实证拒绝恢复取消状态的一个成员；接口需显示member_resume_refused而不是通用解析错误。所有原ID/原始记录保留，未完成团队文件不发布。真实整组初始化/跨进程子成员复用验收仍未通过。
- 下一步策略选择见 [待确认提案](2026-09-08-agent-resume-strategy-proposal.md)；本轮不迁移到会话池、不新建替代成员、不修改CLI内部停止标记。

- 拒绝诊断只接受与本次SendMessage关联的原生success:false，主模型复述的required_response不算子结果。保留取消ID，不自动替换。状态查询显示resume_refused并计入该失败调用已知费用。
- 只读状态也核对同主会话的调用目录；未登记调用或缺失原始调用的孤立修复记录将费用标为未知并阻塞，不能低估为零。已验证的归档空主调用与已知拒绝修复分别识别，避免重复计费。
