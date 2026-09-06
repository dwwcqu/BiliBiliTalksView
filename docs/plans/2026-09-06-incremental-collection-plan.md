# 增量采集核心 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 和测试先行，完成本单元后验收；保持 feat/discussion-cache。

**Goal:** 将已审阅的增量策略接入独立CLI，能够连续刷新、复用楼中楼并恢复同一任务。

**Spec:** [增量刷新设计](2026-09-06-incremental-refresh-design.md)。用户已要求继续实现当前确定任务，本计划落实单元B，不重开标签、发布和保留政策讨论。

## 范围与接口

- 保留 collect 原参数及默认全抓语义；新增 refresh 子命令，mode=auto/full。没有强制快刷开关。
- 新刷新使用独立、明确指定的 --work-dir；可选 --baseline-work（先前任务目录）或 --baseline-batch（协议批次/current.json），二者互斥。无基线首次刷新采用full。
- --resume 只恢复该目录已冻结的任务，不重新读取基线，不改变原模式、时间、预算；提供新基线时拒绝。输出路径继续由 --output 指定，支持Windows/Linux。
- CLI尚无共享小时配额，不能冒充后台缓存；单视频小时归并、PostgreSQL基线版本及worker专用导入由单元C落实。
- 普通协议文件无可信full完成证据，auto首次选择full；工作集保留逐楼核验证据，可连续快刷。
- _refresh 为内部检查点元数据，禁止进入固定v2文件；导出仍提供完整楼与用户分类，保留旧collected_at及partial含义。
- 不访问真实来源、不升级用户数据库、不改变D:/Data样本；所有常规验证使用虚构响应。

## Task 1：冻结刷新基线与恢复入口

**Files:** 新增 backend/app/comment_export/refresh.py；修改checkpoint.py；新增tests/test_refresh_baseline.py。

- [x] 写测试：新目录原子保存基线记录与证据；相同目录拒绝覆盖；冻结后删除基线仍可恢复；两种输入互斥；模式/周期校验失败零来源请求。
- [x] 新增read_checkpoint(path)->(records,metadata,progress)，只读SQLite一致事务，不创建不存在文件；读基线任务时沿用.collect.lock。
- [x] refresh(url,work_dir,client,max_requests=12000,resume=False,*,baseline_work=None,baseline_batch=None,mode='auto',full_interval_hours=24)准备后调用collect(resume=True)。先完整读/校验基线，再用一个检查点事务写入新批次成员、metadata及初始progress。
- [x] 源身份以来源解析结果再次核对，不能将另一视频的旧评论并入新视频；保留冻结摘要和旧观测时段，captured_from覆盖继承记录的真实时间。
- [x] 文件输入复用freeze_batch，逐楼读取coverage；内部上下文不从任意文件扩展推断。只传递验证过的记录，未知作者/未分类记录保留。

## Task 2：逐楼证据、选楼与覆盖合成

**Files:** 新增incremental.py；修改collector.py、availability.py、recovery.py、export.py；新增tests/test_incremental_collection.py。

- [x] 测试连续full→auto→auto，旧楼回复接口连续两次零调用，但所有主楼页仍完整遍历；时间过24小时或上次full未结束时选择full。
- [x] 初始化新刷新时清除上一轮页号、游标及seen，不清除历史最后完整核验依据。主列表只以本轮观察ID检测重复页，不能把基线旧ID当重复数据。
- [x] 新楼、变化楼、真实needs_check重读；complete且count/content无信号变化复用。未在主列表出现的不可用楼等待full；重新出现即核验。选择后的跳过范围用skip_refresh单独表示，恢复目标选择也遵守它。
- [x] 每个有效页事务一起保存观察ID和逐楼证据；核验完成设complete，主动复用保留complete；失败或无可信依据设needs_check。保留身份冲突阻塞、有限复核、请求预算和全局访问限制。
- [x] 输出合成集合包含旧记录；只要存在继承未观察回复，该楼partial/replies_incomplete；未观察旧根使main_incomplete。源端遍历完成单独记录，可更新full完成时间，不把partial当下一轮必须全抓。
- [x] 增加新增/正文变化/小字段更新/复用统计；统计只基于实际观察和冻结摘要，不声称未读取旧内容已核对无变化。
- [x] 核验少量完整样例可导出并通过既有validate_batch；_refresh不得出现在manifest或下游JSONL。

## Task 3：CLI、恢复与回归验收

**Files:** cli.py、README.md、tests/test_comment_export_cli.py、tests/test_incremental_collection.py。

- [x] 接入refresh参数及安全错误输出；退出码0仅verified，完成但partial仍返回3且输出路径可用。恢复已有task无需基线文件，复用已持久化模式。
- [x] 测试10条变9条仍保留10条并partial、新楼/旧楼新增回复、同数量ID替换需full才查明、未知作者/未分类、受限后恢复不会错误选择跳过楼。
- [x] 测试多主楼页均是基线ID时仍到尾、分页中断后继续未完成目标、不重复消费已提交页、身份冲突保留旧数据。
- [x] 运行python -m pytest backend/tests、python -m ruff check backend scripts；数据库使用既有隔离测试配置，不访问用户数据。
- [x] 子Agent审阅、修复并复核；更新本计划状态及README，提交当前单元。后续任务范围仍为单元C/D的已审阅设计，不自动扩展页面、模型或部署。


## 完成状态

单元B已实现，代码提交a7182c2。refresh支持工作集/文件基线、auto/full、同任务恢复及完整协议导出；源端完成标记与导出partial分离，首次解析前失败也能保留请求意图和预算恢复。内部证据不会从协议扩展中被信任，合法未知扩展仍保留。

全套268项后端测试通过，含隔离PostgreSQL回归；Ruff和子Agent复核通过。两条现有FastAPI/Starlette依赖弃用提示仍存在。本轮未访问真实Bilibili、未升级现有开发数据库、未修改D:/Data样本。

后续单元C负责数据库基线版本、逐楼证据入库与worker原子物化、共享小时任务和来源闸门；单元D接HTTP。当前CLI结果不代表这些网站后台能力已完成。
