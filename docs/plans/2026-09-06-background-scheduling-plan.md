# 持久队列与 Worker Implementation Plan

> 使用 superpowers:executing-plans、测试先行及独立审阅，保持 feat/discussion-cache。

**Goal:** 完成C2后台调度库与本地CLI，串联增量采集和C1物化。
**Spec:** [后台设计](2026-09-06-background-cache-design.md)、[数据交接](2026-09-06-background-handoff-plan.md)。本阶段不接HTTP/页面、不部署、不采真实视频。

## 任务与约束

- [x] Task1：新增app/jobs/schema.py、repository.py、errors.py及0004_collection_jobs迁移。resolution_requests按URL/小时唯一；collection_jobs按视频/小时唯一且每视频最多一个活动任务；source_runtime保存来源闸门和worker身份。实现提交、刷新、别名归并、读取和管理意图。测试并发唯一、缓存零来源请求、原接受小时、结果过期和导入缓存新鲜度。
- [x] Task2：新增ownership.py、runner.py；单worker会话锁、60秒租约、10秒独立心跳、2秒空闲轮询。验证原会话仍持锁，请求前/页提交前/物化前检查所有权及取消。首次冻结基线、模式和工作集后才能访问来源，恢复不改小时或预算。
- [x] Task3：解析最多6请求，采集默认12000；网络/5xx最多两次续跑（60/300秒）。认证/429/403/412等拒绝暂停来源；管理员明确recover/revalidate才即时复测，取消不解除闸门。复用现有共享冷却，不增加绕过方式。
- [x] Task4：结束时生成临时协议批次并由C1原子物化。guard在同事务锁job并核对租约/取消、保存结果；不先发布文件。工作根data/jobs按UUID划分，只清理已登记且无活动执行者的旧工作集，闸门引用的诊断数据保留。
- [x] Task5：新增jobs/cli.py与maintenance.py。本地submit/refresh/video/job/request/cancel/retry/recover/source-revalidate及worker --once/循环；不输出凭据、正文或内部令牌。7天轻量元数据清理不得删除当前小时/仍被引用记录。HTTP令牌和客户端配额由D单元接入。
- [x] Task6：假来源全链路、失权、取消与提交竞争、重试预算、来源闸门、清理和缓存连续性测试；全部backend/tests使用隔离PostgreSQL，Ruff及独立审阅通过后提交，更新README。实际开发库升级另行核验。


## 实施决策与完成状态

C2已实现，代码提交cc9574c。新增collection_requested布尔标记，防止清理历史任务后普通访问被误判成首次采集。队列容量使用独立事务级advisory互斥，避免与原子发布的视频/runtime锁倒序；解析意图转job复用原容量槽。解析和采集提交均在事务内核验部署身份及实际会话锁。

313项后端测试通过，包含真实隔离PostgreSQL及假来源worker全链路；Ruff与子Agent复核通过。仅保留两条现有FastAPI/Starlette依赖弃用提示。没有启动真实采集worker、没有升级现有开发库、没有修改D:/Data样本。

C2管理命令仅用于本地服务器。D单元仍需HTTP入口、管理员Token校验、按客户端流量/意图配额及接口分页验收；E单元再接基础输入和讨论页面。整个feat/discussion-cache尚未完成，因此暂不合并main。
