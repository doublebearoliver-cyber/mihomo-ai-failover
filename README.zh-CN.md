# Mihomo AI Failover

面向 macOS、Clash Verge Rev 和 Mihomo 的 OpenAI 专用自动容灾工具。
它保留仍然可用的当前节点，不做“延迟最低优先”；只有真实 OpenAI
路径连续出现两轮可验证硬故障，才切换专用 AI 代理组。

> 当前版本：`0.1.0` 公开预览。默认继续使用 macOS 系统代理，不启用 TUN。

[English](README.md) ·
[AI 使用契约](plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md) ·
[Agent 接入](docs/agent-integration.md) · [架构](docs/architecture.md) ·
[验收清单](docs/validation.md)

> **AI Agent 在调用任何 MCP 工具前，必须先完整读取
> [`openai-network-failover` Skill](plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md)。**
> 这是面向模型的权威使用契约，定义了适用环境、安全边界、工具顺序、停止条件
> 和结果输出要求；不能只根据本 README 自行操作。

## 它解决什么

- ChatGPT 页面打不开、登录链路异常或流式输出中断；
- Codex 桌面端出现可验证网络错误；
- 普通网站仍正常，但当前出口不再能访问 OpenAI；
- 大量订阅节点中缺少按真实出口去重、稳定优先的自动切换。

Codex 暂时没有输出、一次延迟升高、一次偶发失败和 Cloudflare
浏览器挑战都不会单独触发切换。只有在 API、认证和 WebSocket 传输正常时，
精确识别到的 ChatGPT Cloudflare 挑战才记为可候选的
`browser_ambiguous`；其他软响应记为 `soft_unstable`，不能进入候选。

当自动探针只能看到 Cloudflare 挑战、但用户已经用真实浏览器验证登录结果时，
可以把结果按“出口 IP + ASN + 地区”指纹限时记录：成功默认保留 7 天，失败
默认排除 24 小时。反馈不会单独触发切换；出口指纹变化或有效期届满后会重新
评估，避免按节点名称永久拉黑。

## 核心行为

- 每 10 秒检查 OpenAI API、认证、ChatGPT 网页和 WebSocket 传输路径；
- 连续两轮硬故障后才切换；两轮可以来自不同关键目标，但都必须通过本地网络
  和控制器守卫，且至少间隔 8 秒；
- 每轮只对硬故障目标延迟 1 秒复核一次；复核恢复就不计故障，健康目标不重复
  请求；
- 第一轮硬故障后立即并行在隔离 Mihomo 中准备候选，不阻塞第二轮确认；候选
  必须在 120 秒窗口内取得两次相隔至少 5 秒的完整路径成功，其中至少一次
  无需硬故障重试，最后一次结果不超过 30 秒；默认准备 3 个独立出口、最多
  真正试切 2 个；进入提交阶段后直接复用已有候选，不为凑满 3 个阻塞，2 秒
  即时预检失败且从未被选中的候选也不占试切额度；
- 先排除本地断网和 Mihomo 控制器不可用，避免盲切；
- 活跃、温备、冷备三层池按真实出口 IP 去重，并分散 ASN 和地区；
- 候选排序先看健康、成功率、不同出口、不同 ASN、冷却和稳定性，
  最后才看延迟；
- 每次选候选前先通过实时 Mihomo 内核做一次 2 秒即时预检；通过后才选入 AI
  组，等待 3 秒并做完整在线复验；若复验靠硬故障目标重试才恢复，间隔 3 秒
  强制追加一次完整复验，追加复验必须首轮干净通过，否则立即回滚旧节点，且
  不关闭任何连接；
- 复验成功后，关闭所有不经过新节点的旧 OpenAI 连接，不影响普通网站，
  并进入 60 秒观察期；
- 原节点至少冷却 5 分钟，恢复需连续成功；
- 只有活跃、温备和冷备中的独立出口均被本轮验证耗尽后，才判定全部不可用；
  同一故障期只通知一次并进入退避；
- 成功切换会显示 macOS 通知，例如
  `日本 01 → 美国 03；原因：连续两次硬故障（timeout）`。

默认 AI 域名后缀只有：

```text
openai.com
chatgpt.com
oaistatic.com
oaiusercontent.com
oaistatsig.com
```

GitHub、Git、npm、Docker 和普通网站不会成为 OpenAI 切换触发条件。

## 前提

- macOS；
- Clash Verge Rev + Mihomo；
- 当前使用 macOS 系统代理；
- Mihomo 外部控制器使用本机 Unix socket，并设置密钥；
- [`uv`](https://docs.astral.sh/uv/)。

项目不会替用户自动开启 TUN，也不会把 External Controller 暴露到网络。

## 安装

推荐从固定版本安装独立 Python 环境：

```bash
uv tool install \
  'mihomo-ai-failover[mcp] @ git+https://github.com/doublebearoliver-cyber/mihomo-ai-failover@v0.1.0'
```

先只读诊断和预览：

```bash
mihomo-ai-failover diagnose
mihomo-ai-failover check
mihomo-ai-failover profile-preview
```

确认预览正确后，安装持久化 Groups/Rules enhancement 与用户级
LaunchAgent：

```bash
mihomo-ai-failover install \
  --confirm INSTALL_MIHOMO_AI_FAILOVER
```

如果结果包含 `restart_required: true`，先重启 Clash Verge，让它重新生成
运行配置，再执行：

```bash
mihomo-ai-failover check
mihomo-ai-failover inventory
mihomo-ai-failover service-start
mihomo-ai-failover service-status
```

`inventory` 使用一个只监听本机、配置只存在于临时目录的独立 Mihomo
进程扫描候选，不会为了扫描而改动当前 AI 组。隔离扫描器复制实时 Mihomo 的
IPv6、DNS 和 hosts 路径语义；这些设置变化后会使旧候选证据失效并重新验证。

## Codex 插件

```bash
codex plugin marketplace add doublebearoliver-cyber/mihomo-ai-failover
codex plugin add mihomo-ai-failover@mihomo-ai-failover
```

插件提供 `openai-network-failover` Skill 和本地 stdio MCP。启动器优先
复用已安装的 `mihomo-ai-failover-mcp`；找不到时才通过 `uv` 从固定
`v0.1.0` 标签获取。它不会开启 TCP 监听。

## Claude Code 插件

```bash
claude plugin marketplace add doublebearoliver-cyber/mihomo-ai-failover
claude plugin install mihomo-ai-failover@mihomo-ai-failover
```

Codex 和 Claude 共用同一套安全工作流与 15 个 MCP 工具。写工具默认禁用；
即使启用，也必须提供服务器在代码中校验的精确确认词。详见
[Agent 接入说明](docs/agent-integration.md)。

不原生支持插件、但支持本地 stdio MCP 的 Agent，也可以加载同一份
`SKILL.md` 作为操作指令，再按 Agent 接入说明配置 MCP。Skill 本身不会让
云端模型获得本机权限；实际操作仍必须经过受信任的本地 MCP 客户端。

## 常用命令

| 命令 | 是否改动状态 | 用途 |
| --- | --- | --- |
| `diagnose` | 否 | 检查路径、控制器、系统代理、AI 组和 LaunchAgent |
| `check` | 否 | 检查本地网络及真实 OpenAI 路径 |
| `status` | 否 | 查看当前出口状态、监控状态和三层池数量 |
| `profile-preview` | 否 | 预览持久化 Groups/Rules 修改 |
| `inventory` | 仅本地状态 | 建立或刷新独立出口清单 |
| `web-feedback` | 仅本地状态 | 记录限时、按出口指纹绑定的真实浏览器结果；不会切换节点 |
| `run-once` | 可能切换 | 执行一轮正式监控逻辑 |
| `daemon` | 可能切换 | 长期运行监控 |
| `service-start` / `service-stop` | 是 | 启停用户级 LaunchAgent |

所有命令支持 `--config /绝对路径/config.yaml`。

真实浏览器反馈是显式写操作。先停止监控，确认浏览器结果，再记录并重启：

```bash
mihomo-ai-failover service-stop
mihomo-ai-failover web-feedback \
  --node '节点显示名' \
  --status confirmed \
  --reason browser_login_success \
  --confirm RECORD_WEB_FEEDBACK
mihomo-ai-failover service-start
```

失败时把 `confirmed` 改成 `rejected`，原因可写
`browser_login_failed`。没有已扫描的出口指纹时命令会拒绝写入；守护进程仍在
运行时也会拒绝，避免并发覆盖状态。

## 持久性

安装器不会修改会被 Clash Verge 重新生成的 `clash-verge.yaml`。它会：

1. 找出 `profiles.yaml` 当前订阅；
2. 复用当前 Groups 和 Rules enhancement 文件；
3. 没有 enhancement 时才新建；
4. 添加一个 `select` 类型的专用 AI 组；
5. 将五条 AI 域名规则放入 Rules enhancement；
6. 每次写入前创建带清单和 SHA-256 的备份；
7. 原子写入 enhancement，最后才写 `profiles.yaml`。

因此更新订阅、重启 Clash Verge 和重启 Mac 后仍可保留。路径穿越、
symlink 逃逸、冲突规则和写入期间文件变化都会让安装安全停止。

## 本地文件

默认路径：

- 配置：`~/Library/Application Support/Mihomo AI Failover/config.yaml`
- 状态：`~/Library/Application Support/Mihomo AI Failover/state.json`
- 备份：`~/Library/Application Support/Mihomo AI Failover/backups/`
- 日志：`~/Library/Logs/Mihomo AI Failover/monitor.jsonl`
- LaunchAgent：
  `~/Library/LaunchAgents/io.github.doublebearoliver.mihomo-ai-failover.plist`

状态和日志会包含本地节点显示名、观察到的出口 IP/地区/ASN、限时浏览器
反馈、成功率和切换历史，但不会写入订阅地址、代理密码、节点服务器地址或
控制器密钥。MCP 默认隐藏节点名称，并且从不返回出口 IP。

## 一键回滚

```bash
mihomo-ai-failover service-stop
mihomo-ai-failover profile-rollback \
  --confirm ROLLBACK_PROFILE_INTEGRATION
mihomo-ai-failover service-uninstall \
  --confirm UNINSTALL_LAUNCH_AGENT
```

随后重启 Clash Verge。回滚会恢复最近一次备份；新建的 enhancement 文件会
移动到备份目录中的可恢复位置，LaunchAgent plist 会移动到废纸篓。最后如需
删除命令：

```bash
uv tool uninstall mihomo-ai-failover
```

## 安全边界和限制

- 只控制专用 AI 组，不控制全局代理组；
- 只在同一台 Mac 上运行，不提供远程控制端口；
- ChatGPT 网页中的云端模型不能直接访问本机 `localhost`；
- 0.1 版不提供远程到本地桥接，不以公网控制器代替该能力；
- TUN 只应在同节点 A/B 证明 Codex 绕过系统代理后另行评估，本项目不会
  自动启用；
- 首次插件自举会访问 GitHub 和 Python 包索引；运行时网络目的地及本地数据
  见 [PRIVACY.md](PRIVACY.md)。

## 开发与验证

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/scan_sensitive.py
uv run python -m build
```

真实网络测试必须从只读命令开始。不要在没有明确授权时人为破坏当前节点。

## 许可证

MIT，见 [LICENSE](LICENSE)。
