# 环境搭建范围与验证

本次授权范围：在 D:\BiliBiliTalkView 初始化 Git，建立本地开发与未来服务器部署基础。

采用 React + TypeScript + Vite 前端、Python 3.12 + FastAPI 后端。开发时 Vite 将 /api 转发至后端；生产时 FastAPI 同源提供前端构建产物和 API，Docker 使用单个服务。

步骤：初始化 main 分支和虚拟环境；创建基础页面及健康接口；安装并锁定依赖；配置 Docker、GitHub CI、忽略规则；检查构建、后端测试、格式规则、HTTP 连通性和 Compose 配置。

未来数据约定：用户以 UID 为键并保留昵称快照；评论保存评论 ID、根楼 ID、父评论 ID、发言用户 UID 与可确认的回复目标 UID。所有 Bilibili 标识在 JSON 中使用字符串，避免 JavaScript 大整数精度丢失。未知回复关系不得猜测。采集状态独立记录，空页不直接代表完整。模型判断绑定评论 ID 并提供证据。

本次不实现采集、数据库、队列或模型调用，也不创建 GitHub 远程仓库、不发布网站。后续持久化可引入 PostgreSQL 和数据库迁移，长时间采集在独立任务进程中执行。
