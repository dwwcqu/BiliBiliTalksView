# 独立 CLI 会话池与单任务分析接入

日期：2026-09-08。用户已确认采用独立 CLI 会话池方案并继续实现。沿用 feature/codex-agents-thinks，不重建 worktree，不迁移或删除旧原生会话文件。

## 设计与范围

采用应用层主/worker 关系，每个成员一个普通 UUID。pool.json 离线登记 UUID，不表示 CLI 已创建会话；首次实际调用用 --session-id，此后对同 UUID 使用 --resume。每次投递冻结的角色、规则、背景与协调资料，避免把旧历史当作当前事实。主会话可接受协调任务；worker 直接接收现有 AgentDelivery，程序仍按角色与已冻结任务映射进行投递。保留输入 2.0.0 / 输出 1.0.0 和 UID JSON，不增加标签，不增加网页或自动批量调度。

新增 direct_session 命令模式，只提供 Read，不提供原生 Agent/SendMessage。普通会话输出证明独立于原生子成员证明：校验唯一 init/最终 result、UUID、模型、cwd、CLI 版本、无子调用、严格 JSON 与 delivery 摘要，绝不通过修改原生证据解析器伪装原生成员。

pool.json 保存 video_id、model、provider、main（member_id=main, role=coordinator）、members、cli_state_dir、created_at、prior_estimated_cost_usd 和显式旧新身份映射。会话历史在 sessions/pool-state；调用记录在 sessions/pool-calls。UUID 登记不可变，重复创建参数不一致拒绝。迁移仅发布新 registry/manifest；原 manifest、原 team 和失败证据不改写。agent_id 字段对本适配器表示 worker session UUID，execution.json 必须明确 adapter 和 pool_ref，避免混淆原生 ID。

支出以 pool prior offset 加所有实际调用计入；未知费用、未完成调用、未登记会话调用阻塞下一次付费执行。spend_limit 显式授权，不自动增加；本轮 prior offset 0.5036930000000000343。用户随后明确取消原 1 美元限制，要求优先实现目的并节约；本阶段真实验证暂按累计 5 美元的显式程序准入额执行，必要时先评估再调整。普通 CLI quota 不保证硬账单上限，保留实测超额余量。原会话成功/失败调用均不自动换 UUID；相同 attempt 只读取校验旧记录，不再次调用模型。

## 接口与实施任务

- [x] session_evidence.py: verify_session_output(raw, *, session_id, expected_model, cli_version, cwd) -> dict，返回普通会话最终 JSON 对象。正反例覆盖错 UUID/模型/cwd、嵌套 Agent、多个结果、失败结果、非 JSON。
- [x] session_pool.py: create_session_pool(video, *, video_id, model, provider, members, prior_estimated_cost_usd=Decimal(0), legacy=None) -> dict；load_session_pool(video) -> dict；run_pool_turn(video, member_id, *, request, environment, expected_model, spend_limit, authorized=False) -> dict。members 为 member_id/role/role_sha256 字典列表，内部补 agent_id（即普通 UUID）；main 单独登记。返回 status/result/transport/request/records_path/reused，失败阻塞且保留记录。
- [x] command.py 与 runner.py: CliRequest.direct_session=False，direct 模式禁止 definitions/create_agent，固定 Read，记录 direct_session；旧模式保持兼容。
- [x] pool_binding.py: 从 ready frozen run 的资源与已批准成员清单创建池，显式登记原生迁移来源；bind_session_pool 发布角色到任务映射的新不可变 manifest，不接受进行中的旧任务或篡改身份。
- [x] pool_dispatch.py 与 pool_proof.py: 普通 worker 直接接收 prepare_delivery.message，经 run_pool_turn 后发布独立 adapter 证明，accept_response/save_profile 复用既有严格证据与标签校验。proof.verify_proof 仅按明确 adapter 路由，保持原生校验不变。未初始化普通会话不冒称已可恢复。
- [x] 测试先 RED 再实现。合成测试覆盖登记幂等、恢复同 ID、失败/未知费用、禁止静默替换、任务角色与源 ready、假摘要/结果拒绝、画像落盘和原协议回归。
- [x] 独立审阅、Ruff、相关后端测试。若剩余累计预算可安全容纳，使用用户指定 CLI 做极小普通会话创建/同 UUID 恢复验证；不发送真实采集评论，若不足则明确标记真实验收未完成。

## 验收边界

会话 UUID 与最终画像文件永久保留在应用数据目录；实际 CLI 历史能否恢复取决于历史完整性及厂商清理策略，不能预先承诺。未实现自动任务批量协调；主会话普通调用接口与单任务 worker 闭环是本阶段交付。模型主协调的批量决策和调度在后续阶段接入。


## 已落实的恢复与迁移边界

- 已存在 pool 时，后续批次重用首次登记的 legacy 映射，不以新 registry 重写迁移来源。
- 旧 run 已有 execution.json 或已接受旧适配器结果时，不允许同 run 切换适配器，返回明确诊断；调用方创建新分析 run，复用现有 prepared 输入并绑定同一 pool。旧执行配置不能覆盖。
- 同主 UUID 的旧 registry 仍必须逐项匹配 legacy 成员 ID 和角色；正在执行的成员不能迁移。
- 普通会话解析拒绝 task_* 系统事件及子成员元数据；JSON 正文内 Unicode 行分隔符不作为 JSONL 分隔。
- 新分析批次更新本地角色文件时，沿用 pool 身份，在新 registry/任务包内冻结当前角色摘要；pool 的角色摘要仅记录最初来源，不能因此阻止同 UUID 输入更新。

本阶段已实现上述接口并完成正反例、独立审阅及真实CLI合成闭环：一个主会话、三个工作会话、六个分析任务到UID文件；主/工作会话的跨进程同UUID恢复与重复最终任务不重发调用均已验证。自动批量协调及真实评论质量评估保持后续阶段边界。
