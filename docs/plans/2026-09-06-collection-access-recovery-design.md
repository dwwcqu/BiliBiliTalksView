# 采集访问诊断与受限恢复：设计

## 状态与目标

用户已确认优先补齐访问诊断和受限恢复。本设计已由对应计划实现并完成真实补采验证；来源不提供的单楼内容保留为缺口。沿用feat/discussion-cache，这是既有采集功能的可靠性子任务。

目标：准确保存停止原因，让管理员在正常访问条件恢复后有界复测，并继续同一检查点。不能保证来源永久可访问，也不能把复测成功解释为全部评论已取得。

依据：[采集与缓存设计](discussion-cache-design.md)、[导出协议1.0.0](../references/comment-export-protocol-v1.md)、[产品需求](../references/product-requirements.md)。文件协议、数据库来源覆盖状态不变；诊断字段属于任务控制信息，不进入评论正文或模型上下文。

## 已知问题与方案选择

现有source.py把多种HTTP及业务错误合并为access_restricted，collector只保留总括原因。blocked会阻止--resume，但没有受控复测入口。仅重新登录后清空blocked无法证明请求恢复，也会掩盖结构或身份错误。

采用“离线诊断 → 单次复测 → 同进程受控恢复”。相较反复重试，保留清晰证据；相较人工修改SQLite标记，能校验任务和失败目标，避免恢复错误页。历史任务缺失的错误码不补造。

## 错误记录与分类

在请求边界生成安全诊断，在失败处理事务中和任务停止状态一起提交。每次新失败生成failure_id UUID，替换上一份详细失败；保留累计请求计数，不保存无限历史日志。

| 字段 | 约定 |
| --- | --- |
| failure_id、observed_at | UUID与UTC时间 |
| phase | resolve、signing_keys、main、replies、login_check之一；本地异常用local_validation |
| endpoint | 固定允许的API路径，不保存含查询参数的完整URL |
| http_status、api_code | 已实际取得的整数或null；不得反推不存在的响应 |
| category | network_error、source_unavailable、authentication_required、rate_limited、access_restricted、request_rejected、invalid_response、local_validation、unknown_legacy |
| video_id、target | 可确认的视频身份及下一待请求目标：root_id/page或原样main cursor；解析阶段保存已验证的BV/ep标识 |
| retry_after_at | 根据有效Retry-After解析的UTC时间，缺失或无效为null；不保存任意响应头 |
| checkpoint_revision | 最近提交数据进度的递增版本，预算扣减不改变它 |
| safe_reason | 本地固定原因码，不能直接使用服务端message或异常全文 |

分类优先依据实际HTTP状态：429为rate_limited；401为authentication_required（不等同于确定Cookie过期）；403/412为access_restricted；重定向拒绝跟随并归request_rejected；5xx为source_unavailable；其他非200为request_rejected。合法200响应再检查业务码；仅对已确认语义的端点/码进行具体分类，其余非零为access_restricted并原样保留api_code。数据结构错误为invalid_response，连接/超时为network_error。

公开nav返回未登录状态但仍含有效WBI key的既有行为保持不变；获取签名参数成功不代表登录成功。签名key更新和账号登录是不同状态。

身份冲突、分页重复、计数持续变化等本地数据问题保持独立阻塞，不允许通过一次网络成功解除。诊断输出中主楼cursor只显示摘要，完整cursor仅保存在受保护检查点用于重放。读取到响应后，失败页正文不写入正式评论；合法部分数据仍沿用既有规则保存。

## 管理命令语义

已实现的管理命令：

```text
python -m app.comment_export.cli diagnose --work-dir <具体任务目录>
python -m app.comment_export.cli probe --work-dir <具体任务目录> [--next-pending]
python -m app.comment_export.cli recover --work-dir <具体任务目录> --output <导出根> [--next-pending]
python -m app.comment_export.cli login-check
```

diagnose只读本地：返回实际错误分类/码、阶段、目标页、剩余预算、冷却截止、是否可复测及限制；不联网，也不显示Cookie路径内容、用户评论或请求头。

probe默认针对已持久化的失败请求目标，只执行一个目标请求；不推进分页、不写评论、不清除blocked。可以更新受限诊断和预算，成功保存一份安全摘要，但该摘要不能供未来recover直接解锁。

recover必须重新执行同样的即时复测，成功才在同一进程进入采集。登录检查成功不能替代评论端点复测；其他视频成功也不能替代当前目标。复测和恢复默认使用当前后端配置的凭据，不能由网站普通用户提交Cookie。

login-check仅访问固定nav端点一次，输出logged_in、logged_out、unknown三态，不输出UID、昵称或任何账号资料。未知结构/请求失败不能报未登录；此命令不会自动修改任务状态。

### 请求数量与验证条件

楼中楼复测只请求目标页一次。主楼复测若需WBI key，可先取得key再请求目标页，最多两个请求；解析阶段复测至多两个必要请求（ep解析加视频身份核对）。所有请求都计入预算和全局间隔，任一步失败立即停止，不自动替换目标或进行额外试探。

有效复测必须通过现有适配器与归一化校验：HTTP/业务成功、身份一致、页码或游标结构有效。成功空页只有在元数据支持其位置时才算有效，不能把空响应直接当访问恢复。计数变化交给既有有界复核，不因网络恢复而改为完整。

recover在复测之后仍需重新请求待提交页：复测只提供当次进程的访问证据，正常页事务才更新评论与进度。这一额外请求也计入预算。证明对象包含failure_id、checkpoint_revision、请求目标和本进程client，不能跨任务、跨进程或凭据变更复用。

## 锁、崩溃与状态转换

所有task诊断变更、probe和recover取得该任务已有采集锁；活动采集时返回task_busy。诊断只读可并发，返回检查点中的一致记录。

```text
blocked → probe失败：仍blocked，更新本次实际失败和冷却
blocked → probe成功：仍blocked，仅记录可读摘要
blocked → recover即时复测成功：本进程允许一次恢复入口
恢复后的首个有效分页事务提交：blocked=false，与数据进度一起提交
恢复期间再次拒绝/结构异常：仍blocked，生成新failure_id
```

不先持久化blocked=false再发请求。复测后、首个有效页提交前崩溃，检查点仍blocked，下次需要重新复测。预算预扣事务不得无意清除blocked或覆盖未提交分页状态。解除访问阻塞不会清除身份冲突/上下文缺口，也不重置请求预算和连续重复页计数。

解析/签名阶段失败的恢复证明仅授权进入对应阶段；直到下一个有效数据页提交才清除任务阻塞。确实无数据的合法主楼终止页也必须通过已有覆盖检查并事务提交，不能只凭解析成功解锁。

## 冷却、限速与服务器边界

CLI增加共享控制目录BILIBILI_CONTROL_DIR（默认忽略目录data/collection-control），同一部署的全部采集进程共用。所有请求使用同一来源锁和下一允许时间，最低间隔维持至少2秒；这只是项目的压力控制值，不是来源保证安全的频率。

遇到429、403、412或未知业务拒绝，记录部署级冷却。截止时间取“本次失败后30分钟”和有效Retry-After的较晚者；30分钟是本项目默认保护策略，不代表来源公布的恢复时间。冷却到期只允许管理员再次probe/recover，不自动认为已恢复、不自动重启任务。凭据更新也不重置来源冷却。

请求顺序始终任务锁→来源请求锁，避免反向加锁。持久化最近请求时间和cooldown_until，重启不清零；系统时钟明显倒退时提示clock_inconsistent并停止，不用重启跳过间隔。登录检查同样遵守部署级冷却；它可更新登录观测，但不能证明评论端点允许访问。

认证明确失效时等待管理员在官方登录流程更新后端凭据；程序不自动处理交互验证。本阶段不增加后台轮询/通知、账号池、分布式任务队列或多机锁。部署成多实例前必须将来源预算与冷却移入共享协调层；不能让每个容器各自限速后宣称已全局限速。

network_error/source_unavailable不自动重试；保留可恢复诊断，管理员可按现有预算继续。后续后台任务阶段再确定有限自动重试与告警策略，不把本次CLI恢复等同于全天无人值守可用。

## 旧检查点兼容

旧任务只有access_restricted而没有请求详情时，diagnose明确返回unknown_legacy，http_status/api_code=null。不能宣称知道上一次是429、-352或Cookie失效。

若能从已提交主楼/楼中楼进度确定下一待采目标，管理员使用--next-pending明确选择它：复测的是“下一待采页”，不是声称重放历史失败页。选择目标与当前checkpoint_revision绑定并显示目标摘要。无法确定唯一下一目标时返回legacy_target_unavailable，不猜测URL或手工减页号。

旧检查点的blocked、计数和评论保持原样；首次升级只增加控制元数据，不重建任务或丢弃评论。没有准确旧失败时间时，从首次probe/recover注册旧受限任务开始执行本地保护冷却（diagnose仍只读），不凭文件mtime伪造源响应时间。

## 数据边界与凭据

仅从既有BILIBILI_COOKIE_FILE读取凭据，Cookie只发固定HTTPS API域；更新文件后下次命令重新读取。复测与恢复使用同一份内存凭据，期间文件变化不影响已创建client；需要换凭据时终止本命令，下一次重新复测。

禁止记录Cookie、Set-Cookie、完整响应体、账号资料或未经清洗的异常消息。任务控制数据在忽略目录，仓库只保存设计/计划/代码/脱敏测试。

文件导出失败（如源批次README已被外部修改）和网络访问受限是不同错误，分别呈现。恢复采集不擅自清空或覆盖下游修改的文件，可选择新的--output根。

## 验收与后续

离线测试覆盖HTTP状态与业务码区分、nav登录与签名状态分离、错误字段缺失、Retry-After解析、冷却重启保持、多进程请求间隔、预算预扣、probe不写评论、不接受旧成功摘要解锁、首个恢复页事务失败、凭据未泄漏以及旧任务目标无法确定。

真实验证按阶段授权执行：一次登录检查、一次目标复测、通过后同任务恢复；遇到新拒绝停止，不反复登录/重试。结果只在对话汇报。通过标准是诊断准确、恢复有依据、失败不丢断点，不能把来源必须恢复作为程序测试的通过条件。

审阅本设计后另写实施计划，拆分请求诊断、共享冷却和受控恢复。网站后台采集任务与长期运行监控仍后置，缓存读取在源站不可用时继续可用。

## 实施计划

见 [访问诊断与受限恢复实施计划](2026-09-06-collection-access-recovery-plan.md)，代码已实现，真实复测与补采已完成；资源不可用的处理按计划补充边界执行，覆盖仍如实标为partial。
