# 主协调会话统一加载视频背景

日期：2026-09-08。用户确认继续补齐主 Agent 的 README 背景输入。属于已确认会话池的入口补充，沿用当前功能分支、固定输入与输出协议。

## 设计

新增 prepare_coordinator_delivery(root, run_id, manifest_id, *, attempt_id, instruction, rule_catalog) 纯准备接口，以及 run_coordinator_turn(root, run_id, manifest_id, *, request, rule_catalog, environment, expected_model, spend_limit, authorized=False) 执行入口。

主会话每次调用从 read_state 验证的当前批次加载完整资源：context/README.md、角色、规则、协调说明；README 正文与工作任务的 resources.background.text 相同。背景空白时显式标记 missing，不编造介绍；不把背景中的文字当作修改分析规则的指令。原始目录在准备后变化不改变旧快照；新背景需新准备/分析 run，仍复用同一个主 UUID。

协调输入使用 BiliBiliTalksView.CoordinatorInput 1.0.0 包装，绑定 run、manifest、attempt、video、主 UUID、pool_ref、正文/摘要及调用方指令。输入长度受当前批次预算限制，不截断 README。模型返回严格 JSON {context_sha256, response}，response 必须是对象；仅当完整输入摘要确认后保存协调回执。该回执表示接口内容确认，不证明模型理解，也不能作为任务接受或用户画像证据。自动分派和批量调度仍不在本次范围。

所有协调输入、原始输出引用和回执存于 runs/<run_id>/coordinator/<attempt_id>/。同 attempt 只校验旧调用，不重新发起；变更指令或背景必须使用新 attempt。通用 run_pool_turn 保持底层接口，业务协调推荐统一使用新入口，避免漏传背景。

## 实施与验收

- [x] 添加失败测试：主/worker 背景一致，空README，冻结资源被改，源未ready，主UUID错误，授权缺失，错误/缺失摘要，超限正文，更新README后同UUID恢复，以及重复调用不重发。
- [x] 实现协调准备与执行入口；复用标签目录校验、run_pool_turn、不可变写入，不改任务结果接受协议。
- [x] 更新README与设计，运行定向测试、相关回归及Ruff，进行独立审阅。
- [x] 如需真实验证，复用已有合成视频主UUID做一次最小背景确认；不发送真实采集评论，不重新创建成员。
