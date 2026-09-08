# Claude CLI 适配层实施计划

> 执行流程：superpowers:subagent-driven-development；先写失败测试，再实现与复核。

## 目标与依据

继续用户确认的 Claude CLI 接入，落实既有 [Agent 设计](2026-09-05-claude-agent-management-design.md) 中的进程与结果边界。在 feature/codex-agents-thinks 工作，保留现有输入 2.0.0、输出 1.0.0 契约。

## 本轮范围及设计

增加 analysis_execution 本地库。只接受调用方显式配置的可执行文件、模型、预算、超时、稳定工作目录、主 session_id；新建与恢复显式区分。评论与提示正文通过 UTF-8 stdin 传入，禁止 shell 拼接。使用 print/stream-json/verbose；固定最小工具集合及 bare 模式，关闭隐式 MCP 配置。bare 模式不使用订阅 OAuth，调用方必须提供与所选服务相符的显式环境，不读取或改写用户凭据。

进程层捕获有限大小的 stdout/stderr，超时与输出超限终止本次进程树。流解析在进程退出后进行，不在第一个 result 处提前成功。所有主事件核对 session_id，子事件不冒充主结果；多结果采用最后的主结果，任何主失败拒绝。未返回可信费用记 unknown，不从空值推导零，也不把估算当账单。

每次调用在调用方指定的私有、Git 忽略目录建立不可变 attempt 目录，调用前写 request.json；完成后写 outcome.json。同一 attempt 禁止重复发起；中途崩溃留下 request 表示需人工核对，不自动重试。会话级锁覆盖首次与恢复。保存请求摘要与有限响应，不保存环境变量或命令中的认证信息。调用需要 authorized=True，但这是可信服务内部接口，不是公网授权机制。

结果仅标记 transport_succeeded，绝不建立 ExecutionContext.delivery_verified，不调用 accept_response。子成员身份、规则采用、输入读取证据及调度器仍需要下一子阶段连接与实测；不会伪造已完成的端到端分析。

## 文件与步骤

### 1. 事件解析

- 新增 backend/app/analysis_execution/events.py。
- 接口 parse_events(raw: bytes, *, session_id: str, returncode: int) -> dict，输出 status、error_code、result、estimated_cost_usd（字符串或 null）、cost_status。
- 新增 backend/tests/analysis_execution/test_events.py，验证正常结构化结果、多个结果、错误退出、主会话不匹配、子结果、错误 JSON、缺失/负数/非法费用及无结果。
- [x] 先执行测试验证功能缺失，再实现解析并运行正反例。

### 2. 命令与进程

- 新增 command.py：CliRequest 和 build_command，验证 UUID、路径、有限正预算/超时/输出上限，固定参数，正文经 stdin。
- 新增 process.py：run_process(argv, *, cwd, env, stdin, timeout_seconds, max_output_bytes)；返回 returncode/stdout/stderr/stop_reason。使用真实合成 Python 子进程测试 stdin、非零退出、超时、输出超限及进程树清理。
- [x] 先写测试并确认失败，实施后运行对应测试。

### 3. 调用记录

- 新增 runner.py：execute(request, *, records_root, environment, authorized=False)，默认拒绝；在启动前保存请求摘要、attempt 身份，完成后保存有限响应与解析结论。
- request/outcome 分开不可变发布，拒绝重复 attempt；锁路径按主 session UUID，stderr 不回显到异常消息。生产层不接受任意 argv，不保存环境。
- [x] 正反例覆盖未授权无副作用、重复请求不启动、失败保留、返回结果不产生业务验收。

### 4. 验证与文档

- [x] 独立审阅新增模块，修复明确缺陷。
- [x] 运行新增测试、全部 backend/tests、ruff；记录跳过原因到交付说明。
- [x] 更新 README 与已有设计的状态；提交功能分支，不合并 main。

## 验证命令

```powershell
.venv\Scripts\python.exe -m pytest backend/tests/analysis_execution -q
.venv\Scripts\python.exe -m pytest backend/tests -q
.venv\Scripts\python.exe -m ruff check backend scripts
```

## 验收限制

只运行合成子进程，不发送真实评论、不调用付费模型、不连接数据库。真实 CLI 只读取用户指定 C:\Users\dengww\.local\bin\claude.exe 的 version/help（已确认 2.1.261）。服务器进程组与 Windows 进程树行为分别按平台验证；未在 Linux 实跑的部分明确保留验收限制。单次 CLI budget 不是服务商实际费用硬保证；跨任务预算调度尚未接入。

Windows 清理边界：当前使用受控 PID 的 taskkill /T 和主进程兜底终止，属于尽力清理，不能保证根进程已经退出或子进程脱离树后的全部后代都被终止。终止工具失败、根进程提前退出导致无法确认清理、或读取线程未结束时记录 process_cleanup_unverified；同会话后续 attempt 被阻止，需核对残留进程后人工恢复，不能靠更换 attempt 绕过。缺少 outcome 的中断调用同样阻止自动恢复。尚未提供解除隔离的自动接口。正式无人值守服务部署前需要验收受操作系统约束的进程生命周期（例如 Windows Job Object 或服务进程容器）；Linux 分组终止代码尚未在本轮环境实跑。

调用记录目录约定：调用方统一采用 <video-analysis-root>/sessions/cli-calls 作为 records_root；同一视频/会话不能切换记录根绕过锁与中断保护。传输记录是内部版本，不能代替 runs/<run_id>/execution.json 的业务执行来源。
