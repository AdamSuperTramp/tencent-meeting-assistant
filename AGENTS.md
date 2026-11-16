# 腾讯会议助手项目约定

始终使用简体中文交流和维护文档。

## 项目边界
- 本仓库管理腾讯会议助手的源代码、行为规则、部署模板和脱敏测试。
- OpenClaw Gateway 是运行平台，不把 OpenClaw 的 `node_modules` 或整个安装目录纳入本仓库。
- 私人会议登记、来源归档、导出物、发送账本、运行状态和日志都在 `config/runtime.local.json` 指向的仓库外数据目录；不要复制进 Git。
- 凭据只从 OpenClaw 本机密钥文件读取。不得把 token、app secret、私人会议正文、飞书消息内容或真实导出文件写入仓库。

## 改动和运行
- Codex 每次完成项目改动后，核对差异，提交本次任务的文件并推送到 GitHub 对应分支。同步由任务完成触发，不设定时任务。
- 日常改动应限于本仓库。不要在开发时调用腾讯会议写接口、发飞书消息或改真实会议。
- 修改工具路由、权限边界、接收目标、自动发送条件或幂等逻辑时，先解释影响范围并保留配置回滚点。
- 生产部署通过 `scripts/deploy.sh`，该脚本会备份 OpenClaw 配置和 LaunchAgent 文件，再更新运行入口并重载服务。
- 自动化通过 `scripts/pause-resume.sh` 显式暂停或恢复。不要在普通代码验证中解除暂停。
- 新测试必须使用虚构会议 ID 和合成文本，不读取个人数据目录，不发送网络请求。

## 代码分层
- `src/owner_only_bridge.py`：MCP 入口守卫、路由和来源记录。
- `src/business_export.py`：会议来源策略、资料准备、飞书投递与回读。
- `src/post_meeting_poller.py`：会后结束状态和材料就绪轮询。
- `src/settings.py`：本机路径与身份配置的唯一读取入口。
- `prompts/assistant-rules.md`：OpenClaw 助手行为规则的项目源文件。
