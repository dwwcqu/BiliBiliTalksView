# 讨论与采集 HTTP API Implementation Plan

> 使用 superpowers:executing-plans、测试先行和独立审阅，保持 feat/discussion-cache。

**Goal:** 完成D单元，以/api/v1暴露已实现的队列、刷新与讨论查询；不做页面、模型或公网部署。
**Spec:** [后台设计](2026-09-06-background-cache-design.md)、[调度计划](2026-09-06-background-scheduling-plan.md)。

## 接口约定

- POST /video-requests {url}；GET /video-requests/{id}；GET /videos/{video_id}；POST /videos/{video_id}/refresh {mode:auto|full}；GET /jobs/{id}。
- GET /states/{state_id}/threads；GET /states/{state_id}/threads/{root_id}/comments；GET /states/{state_id}/users/{uid}/comments；未知作者使用/users/unknown/comments。分页默认50、最大200，游标绑定状态及查询范围。
- POST /admin/jobs/{id}/recover|retry|cancel；POST /admin/video-requests/{id}/retry；POST /admin/source/revalidate。仅Authorization: Bearer令牌，ADMIN_TOKEN不传给前端、不接受URL令牌。
- 缓存/复用200，新意图202，输入422，未找到404，状态不可用/已回收410，冲突409，限额429，数据库/必要配置不可用503。状态已回收与从未可用的有效UUID统一410，不新建历史墓碑表。无效请求不回显正文。
- API只返回current及partial working；ready/loading/failed不公开。视频摘要、讨论分页在一致快照内读取，禁止混合状态。任务输出采用白名单，不返回所有者令牌、内部核验证据、服务器路径或凭据。
- 请求体最多8KiB，包括无Content-Length的分块体；链接最多2048字符。JSON禁止额外cookie/path等字段，沿用来源URL白名单。
- 写接口每客户端30次/分钟；任何新来源意图（首次解析/采集或新手动刷新）3次/小时；同意图复用与缓存命中不占意图额度，解析再转job不重复计费。配额在PostgreSQL持久化，新意图配额与入队同事务提交或回滚。
- RATE_LIMIT_SECRET为稳定的后端HMAC密钥，仅存客户端连接地址的摘要。默认忽略X-Forwarded-For；仅TRUSTED_PROXIES明确配置的代理可提供地址链，按右侧可信链解析。uvicorn禁用自身proxy-header改写，由应用统一处理。

## 任务

- [x] Task1：新增app/api/rate_limits.py、schema.py及0005_api_rate_limits迁移；测试并发计数、跨实例持久性、小时/分钟边界和过期桶清理。新增repository的on_new_intent事务回调，旧CLI行为不变。
- [x] Task2：新增api/discussions.py、responses.py、security.py、routes.py；create_app支持注入测试engine和配置，生产engine按配置创建并在应用关闭时释放。分页、公开状态过滤、字段白名单与安全JSON序列化（包含特殊Unicode）均测试。
- [x] Task3：接入Bearer鉴权、限额及有界ASGI请求体处理；错误统一安全响应，403/401/413等不创建任务。HTTP端不创建worker、不调用来源或模型。
- [x] Task4：真实隔离PostgreSQL下端到端API测试：缓存零来源、入队/配额同事务、新job与复用状态码、管理权限、未知作者、游标过期/错配、分块大体积、非法路径字段、异常数据库与特殊字符。
- [x] Task5：全套backend/tests、Ruff及独立审阅通过，更新README、环境样例及阶段状态并提交。实际开发库升级及正式服务器上线仍另行核验。


## 完成状态

D单元代码提交b2f88d4。公开讨论/队列接口、管理员Bearer、持久配额、8KiB请求体上限及一致快照分页已实现。写配额、意图配额及入队共享接受时间；代理头按完整可信链处理，README/AGENTS/Docker入口统一关闭Uvicorn预处理。

最终345项后端测试通过，Ruff、Compose配置检查和子Agent复核通过；仍有两条原有依赖弃用提示。仅使用隔离测试数据库和模拟请求，未升级现有开发库或部署服务。下一单元为E基础页面，当前分支整体仍未到合并main条件。
