# Qoder：科研实验续作助手

本目录提供项目任务书和独立的 PowerContext 验证工具。应用主体需要在 Qoder 中开发，并通过 Qoder Sites 发布；任务书和静态文件检查不能代替可运行项目。

## 在 Qoder 中开始

1. 用 Qoder 打开本目录，将 `QODER_TASK.md` 的完整内容交给 Qoder，保留需求拆解、编码和调试对话。
2. 按 Qoder 生成的项目说明配置和启动 PowerContext 与应用。真实服务地址和 token 只放在本地环境变量或受保护的配置中。
3. 先运行独立服务验证，再运行 Qoder 编写的项目主流程验证。应用必须实际保存并召回实验进展，不能只显示连接成功。
4. 重新启动应用，保持相同 Scope，再次确认原进展和下一步能恢复。
5. 保存真实响应、应用日志、页面截图和关键 Qoder 对话。审核项目后，在个人账号中使用 Qoder Sites 发布 `site/`，可见范围选择“任何人都可访问”。

独立服务验证：

```bash
export POWERCONTEXT_URL=http://127.0.0.1:8000
# 如果服务启用了鉴权，在本地另行设置 POWERCONTEXT_TOKEN。
python3 verify_powercontext.py
```

脚本成功时输出实际 Scope、写入与召回响应，以及 `PASS`。同一 Scope 重复运行是幂等写入，`remembered.entry` 为 `null` 表示该内容已经存在，不代表写入失败。此脚本验证 PowerContext 服务，不验证尚未实现的 Qoder 应用。

## 提交前检查

- 网站由 Qoder Sites 发布，在无痕窗口不登录可访问；不要暂停或删除已提交站点。
- 项目按运行说明可启动，页面能完成保存、召回和重启恢复，不是静态演示截图。
- 展示 PowerContext 的实际作用，包含真实记忆响应或日志。
- 提供完整源码、配置示例、运行说明、验证样例和实际结果。
- 提供 Qoder 关键开发记录及 UID：`01a1876e-63bd-7a90-86af-11e66ea03c61`。
- 提交 Qoder Sites 完整网址、源码仓库和 UID，并上传要求的验证材料。
- 不提交真实 API Key、密码或私有实验数据，不虚构用户反馈和使用效果。
