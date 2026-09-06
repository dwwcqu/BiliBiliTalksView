# 后台数据交接与原子物化 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans、测试先行和独立审阅。沿用 feat/discussion-cache。

**Goal:** 完成后台单元C的第一项可独立验收交付：数据库基线冻结、核验证据持久化、版本保护和原子结果写入。

**Spec:** [后台设计](2026-09-06-background-cache-design.md)、[增量设计](2026-09-06-incremental-refresh-design.md)。现有A/B已完成；本步骤不创建队列、worker进程、HTTP或页面，不运行真实采集或升级用户数据库。

## 数据与事务约定

- 新增videos.cache_version非负BIGINT及discussion_states.refresh_context可空JSONB。0003迁移通过数据库触发器在current/working指针变化及所指状态的lifecycle/refresh_context变化时递增版本，覆盖旧CLI导入/发布路径；无实际变化不递增，禁止版本倒退。
- refresh_context保存job_id、批次摘要、冻结时基线版本与精简核验证据。仅保留下一轮选楼需要的状态、时间、比较摘要及观察ID，不复制main_core、observed_core、正文或原始响应。普通文件导入不信任外部_refresh扩展，内部上下文为空。
- prepare_handoff(frozen,records,metadata,job_id,baseline_version)校验工作集和已冻结文件的记录、视频、时间及逐楼覆盖一致，返回不可变Handoff。协议版本/导出ID可因导出而改变，正文、身份、关系和观测时间必须一致。上下文与job/video/batch_digest/base_version绑定。
- materialize(conn,frozen,handoff,commit_guard)持原视频会话锁，在单个事务中核对版本、调用原有导入/发布流程、写上下文并调用commit_guard(conn,result)。guard供后续worker锁job行并核对所有权/取消、同步任务结果；此处不虚构任务表。guard失败或任何加载错误整体回滚，不提交failed/loading半成品。
- 旧import_batch与CLI保持原分批模式；仅内部受控入口开启单事务模式。正文载荷继续复用，partial仅替换working。相同批次/相同handoff可幂等返回；同批次不同上下文拒绝。
- freeze_baseline(conn,video_id,target)从一致数据库快照选择最新可读current/partial working，复制全部记录、未分类数据与内部证据到新的SQLite工作集，返回state_id/cache_version。复制后数据库状态可回收，恢复不依赖旧引用。无可读状态返回无基线和当前版本，不创建虚假完整状态。

## Task 1：版本和上下文迁移

**Files:** schema.py；新增migrations/versions/0003_cache_handoff.py、tests/storage/test_cache_version.py。

- [x] 增加旧库升级/降级测试；验证初值、指针更新、可见状态变化递增，不变更新不递增，回滚不递增，旧正文及引用不变。
- [x] 实现冻结DDL及触发函数，schema与迁移一致；不执行用户开发数据库迁移。

## Task 2：复用导入代码的单事务入口

**Files:** importer.py、publication.py；新增storage/handoff.py、tests/storage/test_handoff.py。

- [x] 新增失败测试：仅有partial缓存时，加载中另连接仍可见旧状态；加载失败及guard拒绝都保留原指针/记录/receipt/version。
- [x] 提取持锁导入函数；显式区分每块提交与调用方事务，禁止Connection代理、吞异常或自动提交调用方事务。发布函数同样支持内部调用方事务。
- [x] 验证context绑定、重复导入幂等、不同job/摘要拒绝、过期baseline_changed、shared payload无损回收。guard只在数据校验完成后执行，事务内结果与上下文一起提交。

## Task 3：数据库基线冻结

**Files:** 新增storage/baseline.py、tests/storage/test_baseline.py；复用Checkpoint与增量prepare_refresh。

- [x] 验证latest partial优先作为刷新基线，但普通read_state仍优先完整current；loading/failed/ready不可作可见基线。
- [x] 快照复制、SQLite原子保存、禁止覆盖已有任务。覆盖UID/未知作者/特殊字符、未分类记录及精简核验证据。
- [x] 回收数据库旧状态后，本地基线仍可读；物化时版本不同必须拒绝覆盖。普通文件导入无可信full证据，下轮auto保守full；worker上下文保留后允许连续快刷。

## 验收与后续

- [x] 独立PostgreSQL测试及全部backend/tests通过，Ruff通过，子Agent审阅并修复；只把完成状态写入计划，不创建仓库验证报告。
- [x] 更新README说明新接口和0003迁移边界，提交本交付。随后以此接口制定并实现队列/worker详细计划；本步骤不宣称后台调度或网站接口已完成。


## 完成状态

C1已完成，代码提交eef0c0b。基线版本、精简核验证据、原子物化和数据库基线冻结接口已通过验收；临时SQLite写完关闭后再原子发布，复制失败可在同目标重试。普通CLI仍保持分批模式。

最终282项后端测试通过（含真实隔离PostgreSQL），Ruff和子Agent复核通过；保留两条现有依赖弃用提示。现有开发数据库未升级、未运行真实来源采集、未改D:/Data样本。

队列、任务领取/租约、worker进程及小时资格仍未实现，接下来用这些数据交接接口完成C2调度部分。commit_guard的具体任务所有权及取消校验由C2提供，本单元仅验证其原子提交/拒绝契约。

后续进展：C2已在[调度计划](2026-09-06-background-scheduling-plan.md)完成，包含实际commit_guard、队列与worker调用；HTTP单元尚未接入。
