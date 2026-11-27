# 架构与运行说明

## 运行路径

1. 飞书消息经 OpenClaw Gateway 的 Feishu WebSocket 进入 Agent。
2. Agent 将腾讯会议工具请求发给本机 stdio MCP `owner_only_bridge.py`。
3. 入口守卫核对当前 OpenClaw 通道、Agent 和工具边界；随后调用官方腾讯会议 MCP 桥接。
4. 导出和投递逻辑在 `business_export.py`：从已知归档或官方 MCP 读取材料，依据登记策略生成 Markdown，调用 OpenClaw Feishu 发送并回读核验。
5. 独立 LaunchAgent 定期启动 `post_meeting_poller.py --once`。轮询器按来源记录和本地开始时间选择候选会议，向腾讯查询明确结束状态，然后检查逐字稿及录播分享链接，按账本控制重试和重复发送。

## 数据与配置分离

- 代码和行为规则：本仓库。
- OpenClaw 生产配置：`~/.openclaw/openclaw.json`。
- 助手工作区规则：`~/.openclaw/workspace/AGENTS.md`，由仓库 `prompts/assistant-rules.md` 管理。
- 会议登记、来源记录、缓存、发送账本、轮询器状态和审计日志：`runtime.local.json` 指向的数据目录。
- 腾讯/飞书凭据：OpenClaw 本机密钥文件，不纳入仓库。
- 导出材料：OpenClaw media 目录，不纳入仓库。

## 当前会后策略

本机配置为 live 自动发送。轮询器每 60 秒唤醒一次；每场会议只在本地有可信开始时间时进入结束检查。开始时间 +20 分钟后按 10 分钟间隔查询，只有腾讯返回明确结束状态才进入材料就绪阶段；固定最长等待窗口为 4 小时。确认结束后，逐字稿和录播卡片分别核验与发送，最多尝试至 30 分钟。目标为配置的「AI创业小组」。私人/pending 会议默认不进入自动发送；录播查看权限由腾讯会议现有设置决定。

## 维护规则

- 先修改和审查 Git diff；不把运行数据或本机配置变更混入代码提交。
- 任何服务路径变更都通过部署脚本生成带时间戳的备份。
- Gateway 与轮询器分别由 macOS LaunchAgent 管理。应保持 Mac mini 不休眠，并维持用户会话；本机 LaunchAgent 配置为 Aqua 会话。
- 出现问题时先运行 `scripts/status.sh`，需要暂停会后自动化时使用 `scripts/pause-resume.sh pause`。回滚后再恢复服务。


## 权限边界

飞书入口由私聊白名单和固定群白名单约束，群聊要求 @ 助手；入口守卫会在配置边界扩大时拒绝 MCP 调用。它不是完整的逐成员数据访问控制：腾讯会议 MCP 使用本机凭据，stdio 调用本身没有独立的可信成员身份。因此，不应扩大群白名单或把工具开放给新 Agent；历史会议的 private/pending/unknown 仍应按本地登记策略限制，成员级隔离尚不能视为已完成。

## 资料进度与补查

get_meeting_delivery_status 返回逐字稿与录播的独立状态，并结合发送账本判断已送达、待核对或尚未检查。历史终态缺少组件记录时显示历史记录不完整。

retry_meeting_delivery 仅在用户要求补查时调用。可选择逐字稿、录播或全部缺项，复用轮询锁、组件缓存和自动发送账本，投递到配置的会后目标群。暂停或发送关闭时拒绝补查。超时后仍可按项补查，已有发送记录优先回读。

本机配置 trusted_group_ids 固定获准群名单，守卫将它与 OpenClaw 实际群名单核对。群请求必须携带绑定会话。群查询按本地公司登记过滤，成员身份仍由 OpenClaw 入口负责。

改期仅在腾讯明确成功后同步，轮询优先读取最新登记时间，并重算尚未确认结束的任务时间。腾讯 HTTP 传输集中在 tencent_client.py，资料进度和补查集中在 meeting_delivery.py。

腾讯响应可能在 HTTP 信封外附加 `_meta.hints` 版本提示，并将原信封移入 `data`。自动导出与 MCP 桥接共用兼容函数，先剥离该包装，再解析原有 `status_code` / `body`；保留旧格式支持和错误处理。若轮询持续报告 `a_no_meeting_row`，应检查响应包装，不能直接将缺少会议条目视为会议未结束。

创建后登记、改期同步及群查询的入口解析同样使用该兼容函数：登记发生在结果展示层解包之前，不能依赖展示层兼容。创建成功后应有 `feishu-created-company-recorded` 审计记录；若为 `feishu-created-company-skip/error`，表示预约与本地自动化登记脱节，不能认定已进入会后轮询。恢复时只从可信的原创建会话、匹配的成功工具回执恢复来源与时间，并先备份登记文件；不能重新预约或把未知会议批量标为公司会议。失败 HTTP / 业务回执不用于登记或改期。

## 企业大脑逐字稿

会后自动化配置中的 `brain_delivery_since` 为带时区的 ISO 时间，删除该键即可关闭。仅接收启用后由自动投递目标群通过助手创建、已确认结束且登记为 company 的会议。后台从 OpenClaw 本机密钥文件读取 `BRAIN_TOKEN`。

轮询器复用准备好的逐字稿 Markdown 和摘要校验值，原样上传到大脑的 `/mcp/upload`（只传 `file` 字段，不再带 `kind`），再将返回的 `attachment_id` 放进 MCP `chat` 的 `attachment_ids`，`message` 说明材料类型与日期，使用 `wait=false` 并省略 `conversation_id`。每份材料开启新会话，自动提交。已交给大脑表示服务已接手处理。飞书回读及录播转码各自独立。大脑投递持有独立锁，检查点短暂获取轮询状态锁并合并写入。

大脑投递进度随轮询状态保存在仓库外。明确拒绝时五分钟后重试，已取得附件编号时复用该编号；缺失回执或写入中断时保留待核对状态。结果不确定时可在大脑网页核对。暂停或 dry-run 时不调用大脑。
