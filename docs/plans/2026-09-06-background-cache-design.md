# 后台采集任务与共享小时缓存：设计

## 状态、目标与边界

用户已确认继续完成feat/discussion-cache剩余目标。本文件先提供设计供审阅，不包含实施授权或任务计划。依据 [总缓存设计](discussion-cache-design.md)、[产品需求](../references/product-requirements.md)、[当前文件协议2.0.0](../references/comment-export-protocol-v2.md)。

目标：提交视频链接后复用缓存或创建持久后台任务；多人同视频只采一次；按北京时间自然小时控制手动刷新；请求结束、网页关闭或服务重启不丢任务与已发布数据。

本子阶段交付任务管理、worker和基础HTTP接口，输入页面与讨论展示另一个子步骤实现。页面美化、模型调用、多机部署和公网正式上线不在本阶段。CLI的--output和现有v1/v2文件兼容不改变，网页用户不能指定服务器文件路径或Cookie。

## 方案与取舍

采用PostgreSQL持久队列、独立单进程worker，第一版不引入Redis/Celery。FastAPI只执行本地校验、数据库查询和短事务，不在HTTP处理函数中采集，也不依赖FastAPI进程内BackgroundTasks保存任务。

worker前台调用现有采集/恢复模块，独立心跳线程只维护任务租约和读取检查点摘要；不再启动脱离管理的采集子进程。整台部署仅一个活跃worker，备用进程取不到部署级会话锁时不领取工作。

首版串行处理一个采集任务直到终止或受限。未知链接的解析也进入该worker，可能等待较长采集结束；队列状态必须显示等待，不能假称处理中。这是降低实现复杂度的明确取舍，后续有实际吞吐需求再设计分片调度。

## 输入、身份与请求归并

仅接受现有支持的HTTPS www.bilibili.com/video/BV... 与 bangumi/play/ep...链接。静态解析先验证域名、路径和长度，去除分享查询参数形成normalized_url；不扩展短链、任意重定向、作品自动发现或其他评论类型。URL最多2048字符，JSON请求体最多8KiB。

已知链接通过video_links直接找到视频，普通提交优先返回可见缓存及活动任务。解析成功后，同时登记来源已确认的BV与ep入口别名，避免这两个常见入口反复解析。

未知链接先创建resolution_request，HTTP立即返回request_id供轮询；同normalized_url同小时归并请求。worker解析成功取得规范video_id后，再在视频锁定事务中决定复用缓存/活动任务或创建首次采集job。两个未知别名可能各解析一次，但解析后必须归并到同一个视频采集任务，不能承诺在知道身份之前消除所有解析请求。

解析失败不假建视频或采集任务；记录安全错误码。重复普通提交只返回同小时已有失败结果，不隐式重试。源站凭据或访问问题交给管理员处理。

## 缓存选择与小时规则

可见缓存优先为current_state；没有current时，仅以最近working中已校验的partial状态作为临时可见缓存。loading、failed和未发布ready不对普通用户显示正文。有current时，最新partial以独立入口提供，不自动替换current。

- 普通提交/查看已知视频，只读取现有缓存或任务，不因跨小时自动采集。
- 没有缓存且从未创建采集job时，首次提交自动创建job；已有失败job时显示失败和刷新入口，不把重复提交当自动重试。
- 用户明确刷新时才判断资格。同视频同小时最多一个逻辑job，已有job则返回它；活动job跨小时也先复用，不并发新建。
- hour_bucket取请求被服务端接受的北京时间自然小时。未知链接即使排队跨小时才解析，任务逻辑requested_at和hour_bucket仍沿用触发该任务的原请求时间；数据库行实际插入created_at单独记录。
- 16:59接受的任务属于16:00；17:00后该任务结束即可主动创建17:00任务，无需等待满60分钟。失败重试仍属于原job及原小时，不重置来源请求预算。
- 返回can_refresh、refresh_block_reason和next_refresh_at。活动任务结束时间未知时，不承诺准确等待时长；跨小时且仍在采集，仍然can_refresh=false。
- 已从CLI导入但没有job记录的视频照常作为缓存。导入本身不创建采集配额记录；同小时的已缓存状态仍视为新鲜，手动采集须进入其下一小时。管理员导入可以更新同小时数据，不能借此自动发起来源请求。

刷新资格、活动任务和job唯一性均由后端事务与数据库约束判断；前端按钮仅展示提示，不承担正确性。来源总评论数和已取得数不作为可刷新条件。

## 增量刷新补充

刷新模式、变化判断、正文载荷复用和未核验范围见 [增量刷新设计](2026-09-06-incremental-refresh-design.md)。不能把已入库记录免于重复INSERT解释为来源不再需要读取旧楼。正文载荷复用及[C1数据交接接口](2026-09-06-background-handoff-plan.md)已实现，包含基线版本、精简核验证据持久化和原子物化。后台任务表、调度器及worker调用已在[C2计划](2026-09-06-background-scheduling-plan.md)完成，HTTP层已由[D单元](2026-09-06-http-api-plan.md)接入，基础页面尚未实现。

## 新增持久对象

| 对象 | 字段与约束 |
| --- | --- |
| resolution_requests | request_id UUID、normalized_url、accepted_at、hour_bucket、status、video_id/job_id可空、安全错误、解析请求计数、owner_token/lease_until/heartbeat_at；(normalized_url,hour_bucket)唯一 |
| collection_jobs | job_id UUID、video_id、requested_at、created_at、hour_bucket、status、phase、attempt_count、not_before、owner_token、lease_until、heartbeat_at、cancel_requested、requested_mode/effective_mode、base_state_id、progress JSONB、result_state_id可空 |
| source_runtime | 固定单行：source_gate=normal/needs_operator/recovering、blocked_job_id或resolution_request_id、暂停原因与时间、worker_token/owner_backend_pid；不保存凭据 |

resolution_request状态为queued、resolving、ready、waiting_source、blocked、failed；解析最多6次来源请求（含最多两次自动续跑），重复提交不清零。身份确认前的请求在resolution_request中计数，身份确认后的采集在job中计数，监控分别显示，不能将解析流量遗漏。

job状态：queued、running、waiting_source、blocked、succeeded、partial、failed、cancelled。phase独立表示resolve/main/replies/export/import。succeeded表示采集和入库发布完成且来源覆盖verified；partial表示本轮已结束并入库但有缺口；blocked表示等待管理员处理，不作为普通失败自动重试。

对(video_id,hour_bucket)加唯一约束，并对queued/running/waiting_source/blocked的video_id加部分唯一约束，确保每视频最多一个未结束job。终态job可保留时间、计数和错误等轻量元数据7天，当前小时及仍被引用记录不能提前清除，不保留历史评论正文。

result_state_id被状态回收时置空，GET旧job返回result_expired=true，不把当前新状态冒充该job原结果。progress只存阶段、请求数、已知楼数、核验楼数、不可用楼数、评论数及更新时间，不复制评论正文或整个SQLite元数据。

## worker领取、心跳与恢复

worker通过专用PostgreSQL连接持有部署级会话锁，领取工作使用短事务与行锁；不得在网络请求期间持有videos行锁。首版每2秒检查一次队列；活动job优先恢复，随后按接受时间处理请求。

每次领取生成新的owner_token，租约60秒，心跳每10秒更新。心跳使用独立连接，不能跨线程共用SQLAlchemy Connection。所有任务状态更新使用job_id+owner_token条件，失去所有权不得继续发布。resolution_request采用相同所有权机制，不能只有job可恢复。心跳还必须通过独立连接检查记录的owner_backend_pid仍持部署级会话锁且worker_token匹配；锁连接丢失则停止续租并通知执行线程，不能由活着的心跳线程让失权任务永久占用。

会话锁与租约共同工作，不能只凭租约过期就抢跑：旧worker连接仍持锁时新worker不得领取。新worker取得部署锁后，对遗留running检查租约和本地任务文件锁；确认无旧执行者才用原检查点恢复。文件锁仍忙则保持等待并报告，不另建工作目录重复抓取。

每次来源请求前、每个数据页提交前及入库/发布前核对执行许可；数据库连接失败、租约失效或owner_token变化时停止，不继续对来源发请求。即使数据库短暂故障，已发布缓存仍可在服务恢复后读取，不能用丢失所有权的worker覆盖结果。

现有collector增加可选任务上下文与取消/所有权回调，CLI默认行为保持不变。任务上下文注入原requested_at/hour_bucket，captured_from/to仍记录实际采集区间。数据进度继续使用已有SQLite逐页事务与revision，PostgreSQL保存的是共享队列和可见评论，不把SQLite当网站缓存。

管理员取消仅设置标志，worker在请求边界停止并保留已提交检查点；关闭网页不触发取消。取消不释放本小时已使用的逻辑job名额，不清除来源访问限制。

## 访问受限、重试与管理员操作

所有worker和管理CLI共用持久BILIBILI_CONTROL_DIR，沿用现有至少2秒间隔及源站拒绝后的冷却。source_busy仅延期调度，不清除blocked、不重置预算。

网络错误和5xx允许最多两次自动续跑，间隔60秒、300秒；仍复用原job/工作目录和总请求预算。预算耗尽转failed或partial并说明原因，不自动提高额度。若检查点仍有访问blocked，则不进入自动重试。

认证失效、HTTP429/403/412或其他未识别业务拒绝将job置blocked、source_gate置needs_operator。冷却到期本身不解除这个闸门；新视频请求可读取缓存或排队，但不能通过新任务规避暂停。

管理员明确请求恢复后，worker按现有流程执行即时复测；正常响应或端点限定的资源不可用得到合规处理后，才能继续该任务。恢复中再遇拒绝保持闸门；受控运行成功结束且没有新的来源阻塞，才恢复normal并处理后续队列。取消被阻塞任务不自动解除来源闸门，仍需管理员对保存的失败目标做有界复核。source revalidate成功只恢复新任务的执行资格，不自动重启已取消任务，也不提升评论覆盖状态；旧失败目标的最小诊断与最后一个工作集在该处理结束前保留。未知目标或本地数据一致性错误不支持用网络复核解锁。

已确认的单楼不可用继续当前做法：保留已有内容、记录缺口、继续其他楼；它不会暂停整个部署。数据完整性错误、未知错误和导出/入库错误不得归类成可无限自动重试的网络问题。

管理员接口使用仅后端持有的ADMIN_TOKEN；不得将其注入前端构建或放在URL中。不新增网站用户注册/密码体系，不允许普通用户替换Cookie、清除冷却或直接修改任务状态。

## 数据物化与发布连续性

worker结束采集或确定停止后，冻结检查点，生成协议2.0.0临时批次并调用校验/存储模块；增量实施后相同正文通过payload引用复用，不再次插入大段正文。只在此时物化正文；运行中页面先显示进度，不每几秒生成成千文件或导入一遍全部评论。

本阶段需增加worker专用的原子导入模式：在事务外准备并校验冻结输入，在一个事务内替换working、写入新状态、复核并完成发布决策。可以分块执行SQL，但不分块提交。否则首次只有partial缓存时，现有分批导入会先删旧working再写loading，导致刷新期间暂时无可读缓存。旧CLI的分批导入模式保留。

该事务在写数据阶段不长时间锁job行，允许心跳继续更新。临近发布时才短暂锁job行，重新核对部署所有权、owner_token、租约和取消标志，再写入result_state_id及终态，避免数据库已发布但job还显示失败。取消先提交则发布回滚；发布先提交则后来的取消返回状态冲突，不能把已发布任务改成取消。失败整体回滚，旧current或旧partial仍可读。verified结果按原规则发布current；partial只替换最近working，有current时仍不自动覆盖它。

所有HTTP读取使用一致快照，并只允许读取当前current或可见partial working。一个请求读完旧状态是合法的，后续分页若状态已回收返回state_expired，调用方重新读取视频入口，禁止拼接状态。

JOB_DATA_ROOT默认data/jobs，子目录仅用job UUID；FILE_EXPORT_ROOT为管理员配置的统一输出根。所有生成目录采用英文稳定标识。输入请求不携带服务器文件路径。

成功物化后按现有状态保留策略清理无引用旧正文。取消、受限任务最多保留最近一个可恢复工作集；新任务替换前确认旧执行者和源闸门已妥善处理。7天任务元数据保留不意味着保留7天评论正文。已由用户手工修改的旧调试目录不纳入自动清理。

## HTTP接口草案（/api/v1）

| 接口 | 行为 |
| --- | --- |
| POST /video-requests，body={url} | 返回缓存、已有job或排队的request_id；只有首次无缓存才创建采集意图 |
| GET /video-requests/{request_id} | 解析状态、规范video_id、所关联job_id；不调用来源 |
| GET /videos/{video_id} | 默认state_id、覆盖/采集时间、活动job、可见partial入口、刷新资格 |
| POST /videos/{video_id}/refresh | 手动刷新，事务判断新建/复用；无网页断开取消语义 |
| GET /jobs/{job_id} | status/phase、计数、更新时间、结果入口与必要限制，不返回lease或文件路径 |
| GET /states/{state_id}/threads | 分页楼索引，包含root_author、回复数量和coverage |
| GET /states/{state_id}/threads/{root_id}/comments | 根评论优先，再按时间/评论ID分页 |
| GET /states/{state_id}/users/{uid}/comments | 用户在该状态中的发言；未知作者使用独立unknown入口 |
| POST /admin/jobs/{job_id}/recover、/retry、/cancel | 管理员控制意图；由worker执行，不在HTTP线程采集 |
| POST /admin/video-requests/{request_id}/retry | 恢复尚未取得video_id的失败解析，仍使用原意图/小时/解析预算 |
| POST /admin/source/revalidate | 针对source_runtime保存的失败目标进行有界复核；用于原任务取消后仍需解除来源闸门的情形 |

缓存命中/复用结果返回200，新排队返回202；非法输入422，不存在404，当前状态过期410，小时资格或状态冲突409，调用限额429，数据库暂不可用503。每个回复包含明确disposition或状态，不能仅靠HTTP成功判定来源内容完整。

评论记录沿用协议2.0.0字段语义；HTTP包络和分页字段单独定义，不把文件协议版本当成整个API版本。默认分页50、最大200；活跃任务建议3秒轮询，终态停止轮询。展示计数及阶段，不用会变化的来源总数制造精确完成百分比。

## 配额与部署约束

首版可配置默认：写接口每客户端30次/分钟，新视频采集意图每客户端3次/小时，部署待处理新视频最多20个。缓存读取/复用已有job不占新采集名额，但仍受HTTP流量限制。未知链接先占一个解析意图名额，别名收敛只创建一个采集job；不能靠大量未知别名绕过来源压力控制。

服务只信任实际连接地址或明确配置的反向代理，不直接信任任意X-Forwarded-For。限额状态与队列容量在PostgreSQL原子更新，进程重启不清零；客户端标识使用本地密钥摘要，普通日志不记录凭据或评论正文。

开发部署沿用本机绑定；web和worker分开进程，worker必须持久挂载工作目录与来源控制目录，使用同一数据库和配置。此设计不等于已完成公网访问控制、备份和目标服务器来源验证。

## 验收标准与下一步

必须验证：缓存命中零来源请求、两个别名最终单job、同视频并发刷新唯一、16:59排队到17:00仍归16:00、跨小时活动job复用、失败不重置预算、进程被终止后从提交点恢复、旧worker失权后停止、partial缓存在原子导入期间持续可读、取消与发布竞争、来源闸门不被新视频绕过，以及过期分页明确失败。

所有常规测试使用假来源响应与独立PostgreSQL，禁止真实Bilibili和模型调用。真实任务联调在实现完成后单独执行并报告覆盖限制。

先审阅本设计，确认后编写后台核心/HTTP接口实施计划；基础输入和讨论页面在后续子步骤接入同一接口，不一次实现所有剩余网页功能。

增量任务的基线冻结、缓存版本比较和worker专用核验证据传递遵循[增量刷新设计](2026-09-06-incremental-refresh-design.md)。冻结工作集包含正文与内部观测，数据库回收旧状态不影响恢复；物化事务同时提交内部证据，协议文件本身不能替代该上下文。

C2补充：队列容量互斥独立于source_runtime行锁；解析转换复用请求容量槽。视频持久collection_requested标记，确保任务元数据过期后不再次自动首次采集。purge保留解析引用与来源闸门引用，先处理工作集再移除任务登记。
