# 基础采集入口 Implementation Plan

> 使用 superpowers:executing-plans、测试先行和独立审阅。沿用feat/discussion-cache，不更改已确认的数据与刷新规则。

**Goal:** 根据用户收缩后的范围完成输入视频地址、提交采集和查看数据库保存状态的页面。
**Spec:** [采集页面设计](2026-09-06-discussion-browser-design.md)。不实现讨论浏览、用户视图或AI页面。

- [x] Task1：新增frontend/src/api.ts，封装submit/request/job/video/refresh五个API，校验关键返回字段，统一超时/中止/错误/429；不访问Bilibili域名。使用Node24内置测试运行器测试纯TS模块，不新增测试依赖。
- [x] Task2：新增frontend/src/capture.ts，集中管理目标、轮询、请求代次和中止；只由提交/更新事件POST，reload/轮询/恢复均GET；丢弃旧响应、处理终态和部分保存、保持任务与视频归属一致。使用可注入时钟/调度器测试。
- [x] Task3：新增frontend/src/App.tsx，main.tsx仅挂载；表单、任务状态、保存摘要、按小时更新按钮和安全错误提示。复用style.css做基础可访问排版，不渲染评论正文或分析结果。
- [x] Task4：接入npm test与CI；npm test及build通过，浏览器可用时用模拟API验证输入/轮询/保存/错误流程，禁止验收时自动发起真实抓取。测试材料仅放忽略目录。
- [x] Task5：独立审阅、修复、更新README与阶段状态并提交。实际数据库升级和真实端到端运行由后续本地联调计划核验，不把测试模拟结果当用户数据。

## 交付边界

前端 21 项测试、TypeScript/Vite 构建、模拟 API 浏览器交互和独立审阅已通过。修复了 GET 限流等待被手动读取绕过及页面隐藏后冷却按钮不能恢复的问题。后续已完成[本地联调](2026-09-06-local-capture-integration-plan.md)，真实任务经页面提交、worker 采集和数据库保存，页面结果已核对；来源不可得内容保留 partial。讨论浏览、AI 页面和公网部署不在本次范围内。
