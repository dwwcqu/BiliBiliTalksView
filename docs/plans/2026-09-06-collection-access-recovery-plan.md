# 访问诊断与受限恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 按复选框记录实际验证，不提前解除真实任务阻塞。

**Goal:** 为现有CLI提供安全诊断、共享冷却、单次复测和同进程受控恢复。

**Architecture:** 请求层保留结构化失败；共享控制器管理来源间隔/冷却，任务检查点管理预算与数据版本；恢复证明只在内存中使用，首个有效分页事务提交才解除访问阻塞。

**Tech Stack:** Python 3.12、现有httpx、sqlite3、跨平台文件锁、pytest/MockTransport。不新增后台调度服务或外部依赖。

**Spec:** [已确认恢复设计](2026-09-06-collection-access-recovery-design.md)。导出协议1.0.0与数据库覆盖语义不变。

## 全局约束与接口

当前feat/discussion-cache；代码实现与离线回归已完成，真实旧任务已只读诊断并登记冷却，尚未发送网络复测。实现后以离线测试和一次有界真实复测分别验收；来源仍拒绝不代表程序测试失败，也不能宣称访问已恢复。

BILIBILI_CONTROL_DIR默认data/collection-control，同部署CLI必须共用目录。任务锁→来源锁的顺序固定。最低请求间隔2秒，访问拒绝后保护冷却30分钟，并取有效Retry-After更晚的截止时间；管理员命令也不能清零预算和冷却。

| 文件 | 新增或调整接口 |
| --- | --- |
| diagnostics.py | FailureDetail dataclass，to_public_dict()脱敏；parse_retry_after(value, observed_at)->UTC时间或None；classify_failure(http_status, api_code, endpoint)->category |
| access_control.py | AccessControl(directory, clock, sleep)，attempt(endpoint,target)上下文管理器、record_failure(detail)、read_status()；AccessClient(httpx.Client)携带access_policy |
| source.py | CollectionStopped兼容reason并增加detail；_request在AccessClient策略上下文中请求/分类；普通httpx测试客户端仍可注入，CLI真实请求统一使用AccessClient |
| checkpoint.py | read_control_snapshot(path)只读事务；commit_page成功后revision递增；记录当前attempt、failure与probe_summary；旧库无revision按0读取 |
| recovery.py | diagnose(work_dir, control_dir)->dict；select_target(snapshot,next_pending)->dict；probe(work_dir,client,next_pending=False)->dict；recover(work_dir,client,output,next_pending=False)->dict |
| collector.py | collect增加仅供内部使用的recovery_proof参数；首个有效页事务消费证明，不能用布尔ignore_blocked开关 |
| cli.py | 新增diagnose/probe/recover/login-check，复用--work-dir、--output和凭据加载 |

FailureDetail字段按设计完整定义，旧记录没有的HTTP/业务码为None。RecoveryProof为内部对象，绑定真实client对象、失败ID、检查点revision和目标，禁止反序列化文件或CLI字符串构造；证明不包含Cookie。

## Task 1：请求级安全诊断

**Files:** 新增diagnostics.py，修改source.py；新增backend/tests/test_access_diagnostics.py，扩展test_bilibili_source.py。

- [x] 先写HTTP429、401、403/412、5xx、重定向和200非零业务码测试，预期具体分类和安全字段；验证失败后再改请求层。

```python
def test_http_429_keeps_status_without_remote_message():
    category = classify_failure(429, None, "/x/v2/reply/reply")
    assert category == "rate_limited"
```

- [x] FailureDetail仅接收允许字段；实际响应状态与实际解析到的整数业务码保存，未知值None。request/response头、Cookie、服务端message、原始正文不得进入对象或异常str。
- [x] Retry-After接受非负秒数或HTTP日期，转UTC；无效值None，过期日期不能缩短本地冷却。注入当前时间测试，不能依赖真实墙钟等待。
- [x] _request在发送前登记固定endpoint与必要目标，在返回数据结构校验失败时保留该次安全上下文。解析后的评论/账号资料不写诊断；主cursor公开输出只保留摘要。
- [x] 保持nav未登录但可取得WBI key的行为；新增登录检查解析，只接受明确isLogin布尔值，否则unknown。不能把HTTP失败当logged_out。
- [x] 用携带虚构secret的响应/异常测试诊断输出不含secret，运行 `python -m pytest backend/tests/test_access_diagnostics.py backend/tests/test_bilibili_source.py -q`，提交 `feat: retain safe collection failure diagnostics`。

## Task 2：共享限速与冷却

**Files:** 新增access_control.py、backend/tests/test_access_control.py；调整cli.py客户端工厂、source.py请求上下文。

- [x] 先写时钟注入测试：两实例共用目录读到同一cooldown，过期前拒绝且不调用网络；读取状态不创建文件。

```python
def test_cooldown_survives_controller_restart(tmp_path, fake_clock, rate_limit_detail):
    first = AccessControl(tmp_path, clock=fake_clock.now, sleep=fake_clock.sleep)
    first.record_failure(rate_limit_detail)
    second = AccessControl(tmp_path, clock=fake_clock.now, sleep=fake_clock.sleep)
    assert second.read_status()["cooldown_until"] == first.read_status()["cooldown_until"]
```

fake_clock是测试专用时钟，rate_limit_detail是虚构FailureDetail，不请求Bilibili。

- [x] 控制目录保存来源锁和小型SQLite控制状态，包含last_request_at、next_allowed_at、cooldown_until；状态更新事务化。source attempt在持锁期间检查冷却、等待允许间隔、预记录尝试、执行请求并分类，finally释放锁；请求失败也占用间隔。
- [x] AccessClient只在CLI工厂创建，传入AccessControl；现有source函数通过client的显式策略属性包裹完整_request（包括业务码检查），防止200业务拒绝漏掉共享冷却。单元测试用独立临时目录/假时钟；不访问默认控制目录。
- [x] 网络/源服务异常不自动重试；记录访问拒绝时cooldown只延长不缩短。时钟倒退超过5秒返回clock_inconsistent，允许范围内等待到原next_allowed_at，不重写为更早时间。
- [x] 多进程测试实际来源锁互斥，网络部分用本地桩；验证异常退出后锁可获取。将间隔值作为测试内部构造参数缩短，CLI生产最低2秒不可通过普通参数降低。
- [x] 运行 `python -m pytest backend/tests/test_access_control.py -q`，提交 `feat: coordinate collection cooldown across CLI tasks`。

## Task 3：检查点诊断、预算与旧任务兼容

**Files:** checkpoint.py、collector.py、backend/tests/test_checkpoint.py；新增test_access_checkpoint.py。

- [x] 新增只读快照入口，用SQLite mode=ro一次事务读取控制状态和必要metadata，不调用会建表的Checkpoint构造器；不存在任务返回task_not_found，不能创建空库。
- [x] 每个新页commit_page成功时revision+1，同一页重复提交、预算预扣、probe摘要不改变revision；数据/下一页/revision同事务回滚。

```python
def test_budget_update_does_not_advance_data_revision(checkpoint):
    before = checkpoint.get_progress().get("checkpoint_revision", 0)
    progress = checkpoint.get_progress()
    progress["requests"] = progress.get("requests", 0) + 1
    checkpoint.set_progress(progress)
    assert checkpoint.get_progress().get("checkpoint_revision", 0) == before
```

- [x] 在任务请求预算预扣事务中保存当前attempt目标及revision，不提前更新页游标。失败处理保存新FailureDetail与blocked，保留已提交数据。诊断结构/metadata更新不能复现旧的未提交页进度覆盖问题。
- [x] 对旧任务diagnose返回unknown_legacy和空HTTP/业务码；--next-pending仅根据已提交进度选下一页，先检验根楼集合/顺序、页码、游标唯一性，无法唯一确定则legacy_target_unavailable。
- [x] 第一次probe/recover注册旧受限任务时生成控制诊断并建立保护冷却，返回cooldown_active，不请求；后续调用不重复延长注册冷却。diagnose不注册、不写文件、不更改mtime。
- [x] 运行相关检查点测试，提交 `feat: persist recovery targets and checkpoint revisions`。

## Task 4：只复测与同进程恢复

**Files:** recovery.py、collector.py；新增backend/tests/test_access_recovery.py。

- [x] 先写probe成功仍blocked且评论/分页/revision不变的测试；只允许预算和安全probe摘要变化。

```python
def test_probe_does_not_unblock_or_commit_page(blocked_task, success_client):
    before = read_control_snapshot(blocked_task)
    probe(blocked_task, success_client)
    after = read_control_snapshot(blocked_task)
    assert after["progress"]["blocked"] is True
    assert after["progress"]["checkpoint_revision"] == before["progress"]["checkpoint_revision"]
```

blocked_task与success_client fixture使用非legacy虚构任务和MockTransport，避免被首次旧任务冷却注册遮蔽测试。

- [x] 抽取任务预算绑定器，collect/probe/recover共用预扣计数逻辑，不叠加两套计数钩子。登录检查无任务时单独限定一次请求，但仍受来源控制器约束。
- [x] 复测严格验证身份、目标页/游标和归一化结果。reply最多一个请求，main加key最多两个，ep解析至多两个；无自动重试。合法空页按现有元数据规则核验，不能仅看code=0。
- [x] recover在任务锁内即时probe，构造不可持久化证明，使用同一client进入collect；失败ID/revision/目标变化或client不同立即拒绝。不能接受之前probe保存的摘要作为证明。
- [x] collector保留blocked直至首个有效页commit_page，证明只允许进入恢复路径；首次成功提交原子写blocked=false并清除本次访问阻塞，后续新错误产生新failure_id。身份冲突、计数异常等不属于可通过网络复测解除的类别。
- [x] 测试复测后进程退出、首次页事务故障、复测后再拒绝、旧证明复用、预算耗尽、凭据文件更新但本client不变；均不得提前解锁或丢进度。只通过URL解析不得解锁。
- [x] 运行 `python -m pytest backend/tests/test_access_recovery.py -q`，提交 `feat: gate collection recovery on a fresh probe`。

## Task 5：CLI、回归与有界真实验证

**Files:** cli.py、README.md、.env.example、test_comment_export_cli.py。

- [x] 新命令按设计接入，--work-dir指具体任务目录；collect仍接受工作根目录，帮助文本区分。--output仍接受Windows/Linux路径，不覆盖下游修改文件。
- [x] diagnose仅输出安全状态；probe输出成功/失败和剩余预算，成功也注明blocked未清除；recover分别输出采集结果与导出结果。退出码0表示本命令目的达成、2参数或本地结构错误、3受限/冷却/未完成、4文件/内部错误。diagnose的0仅表示诊断读取成功，probe的0仅表示本次复测通过；recover仍blocked或partial时必须返回3。login-check仅logged_in返回0，logged_out/unknown返回3。
- [x] CLI帮助和测试说明probe是网络操作、diagnose不联网；所有新增普通测试使用MockTransport和临时控制目录。保留已有采集/文件导出/PostgreSQL测试。
- [x] 运行 `python -m pytest backend/tests -q`（配置独立TEST_DATABASE_URL覆盖数据库回归）、`python -m ruff check backend scripts`，确认凭据/控制数据库/真实内容未暂存。
- [x] 在用户批准实施后的真实验收中，先仅运行现有任务diagnose，核对是否legacy及冷却状态；未到期则明确等待，不改计时。可用时执行一次login-check及显式--next-pending目标probe；失败立即停止，报告具体实际状态码。
- [x] 即时复测成功，或严格匹配已确认的单楼不可用状态后，才受控继续recover，沿用同一任务预算，不清空现有blocked或另建任务规避；源站仍拒绝时记录功能验收通过但真实访问未恢复。导出使用独立输出根，避免原D:/Data已修改README导致第二个错误混淆诊断。
- [x] 提交 `feat: expose collection diagnosis and recovery commands`，在对话交付真实结果，不创建入库排查报告。网站后台任务、定时重试、告警和公网部署继续后置。

## 完成标准

可准确报告停止原因、受控复测和恢复，不丢失任何已提交进度；共享冷却重启有效；普通测试不访问来源；未恢复不能显示完成。真实来源可用性是单独结果，程序正确也可能保持blocked。完成本子任务不等于整个网站功能分支完成，不提前合并main。


## 实施收敛与未完成验收

请求诊断、共享来源控制、检查点revision、只读快照、内存恢复证明和CLI已实现。request_budget.py提供collect/probe共用预算绑定，recovery_gate.py绑定任务、目标、revision、client与包括Cookie jar在内的凭据摘要；摘要只在内存中使用，不写日志或检查点。

旧任务注册先将固定冷却截止写入任务控制记录，再幂等应用到共享控制器，避免中断后重复延长。首个恢复页失败时保留blocked；目标必须仍是当前待采页，临时来源锁/冷却不能永久改变旧任务恢复资格。

真实任务已完成诊断、旧任务登记、到期后的单次登录检查和目标页复测；目标复测未通过，未执行recover，不标记访问恢复。具体响应、截止时间和数量在对话与忽略控制目录记录，不写入仓库报告。


## 资源不可用边界（已落实）

回复端点HTTP200且业务码12022/12006按单楼资源不可用处理；不据此断言永久删除，也不将其自动解释为账号或网络封禁。既有评论保留，单楼与批次仍为partial。只有严格匹配端点、HTTP状态、业务码、任务视频与当前目标后才能标记该楼已处理，其他未知拒绝仍停止整个任务。

## 已确认修正：单楼资源不可用

用户已确认保留不可用楼的既有内容、明确未取得回复，并继续其他楼。实现仅将回复端点HTTP200且业务码12022/12006作为源端不提供该评论资源的终止状态，不将它解释为全局限流，也不断言已永久删除。403/429等HTTP拒绝仍优先作为访问问题处理。现有冷却不手工清除。

实施步骤：先写分类、冷却及逐楼继续的失败测试；将不可用状态与下一待采选择原子保存；recover对即时复测确认的单楼不可用走同一事务，不复用旧成功证明。保留所有已采评论，分页状态仍partial、原因使用协议现有replies_incomplete；遍历结束不提升存在缺口的批次为verified。离线检查后，从原断点继续并导出到独立目录，核对每个楼的终态及实际数量。


## 补采验收完成

已从原检查点处理完本轮主楼清单：每楼均为已核验或源端不可用终态，没有未处理目标。保留不可用楼已有内容和分页缺口，最终批次仍为partial；文件双重归类校验通过，同一批次已同步现有PostgreSQL。最新导出位于本机参数指定的D:/Data/access-recovery，原下游编辑目录不变。

此完成表示本轮可读取范围补采结束，不保证源站的瞬时总数或已删除/隐藏内容可得。后续新评论刷新属于下一次采集状态，不以后台无限轮询追求数量相等。
