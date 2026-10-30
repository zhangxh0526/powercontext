请继续本次项目开发。先阅读以下实际复核结果并修复，再完成尚未完成的站点和交付文件。不要使用子代理。

## 必须修复：快速连续保存会召回旧进展

真实公开 API 复现工具和报告已经放在：
- /home/legend/seekdb-competition/qoder-records/review-save-order.py
- /home/legend/seekdb-competition/qoder-records/save-order-review.json

工具启动独立应用端口 18901，实际调用当前 PowerContext。每组创建独立实验，连续保存 FIRST/STEP-A 和 SECOND/STEP-B，再询问“上次进行到哪里，下一步是什么？”。8 组中有 4 组返回 FIRST。两个保存的 saved_at 完全相同，示例为 2026-10-30T02:40:19Z。

当前 utc_now() 只精确到秒，recall() 在时间相同的情况下按随机 entry ID 选择 latest，导致该错误。修复的外部行为要求：连续成功保存后，无论是否发生在同一秒，召回和应用重启后都能明确恢复最后一次成功保存的进展与下一步。不能靠给测试添加 sleep、降低断言或把本地缓存当作 PowerContext 召回结果。也请覆盖一个实验超过六次保存后仍能恢复最新成功保存的场景，确保召回候选数量不使旧记录取代新记录。

请补充最小必要的真实 HTTP 回归，并保留先前真实失败报告，不要把失败目录改写为成功。

## 验证方式

本轮只通过文件、命令行和真实 HTTP API 开发与验证。不要启动或控制 Chrome、Chromium、Playwright、CDP、Selenium 或桌面自动化；页面验收与截图由调用方通过已连接浏览器完成。若 verification/browser.py 已创建，请不要执行，并在交付说明中不要声称本轮已完成独立浏览器页面验收。

## 交付

继续完成 QODER_TASK.md 中的可运行项目、site/ 展示站点、配置示例、启动停止说明、真实响应证据和开发说明。当前参赛 Qoder 账号已经登录，UID 为 01a1876e-63bd-7a90-86af-11e66ea03c61，Qoder Sites 查询可用。先完成可审阅的本地项目，不要发布、推送 Git 或改变账号权限。请继续执行工作而不是只提出方案。
