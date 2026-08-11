# Mihomo AI Failover — ChatGPT、Codex 与多模型代理自动容灾

面向 macOS、Clash Verge Rev 和 Mihomo 的多 AI Provider 自动容灾工具。
它保留仍然可用的当前节点，不做“延迟最低优先”；只有某个 Provider
的真实路径积累到经过守卫的可验证硬故障证据，才切换该 Provider 自己的代理组。

> 当前版本：`0.2.2` 公开预览。默认只启用 OpenAI；WorkBuddy（国内版）、
> Kimi、MiniMax 和 Mavis 必须先在本机发现并审核真实连接，再单独启用。
> 默认继续使用 macOS 系统代理，不启用 TUN。

[English](README.md) ·
[AI 使用契约](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/SKILL.md) ·
[Agent 接入](docs/agent-integration.md) · [架构](docs/architecture.md) ·
[验收清单](docs/validation.md)

> **AI Agent 在调用任何 MCP 工具前，必须先完整读取
> [`dbear-mihomo-ai-failover` Skill](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/SKILL.md)。**
> 这是面向模型的权威使用契约，定义了适用环境、安全边界、工具顺序、停止条件
> 和结果输出要求；不能只根据本 README 自行操作。

## 两层版本，不维护两个分叉

- **公开版**：通用引擎、五个保守 Provider 模板、CLI、MCP、Skill、安装、
  回滚和测试；不包含任何用户的节点、出口或本机观察域名。
- **个人版**：同一套代码加本机私有 `providers.local.yaml` 覆写；记录这台
  Mac 已审核的 Provider 开关、精确域名和关键探针。文件权限为 `0600`，
  位于应用数据目录，Git 默认忽略。

这种结构避免私人分叉长期落后于公开版。其他 Agent 不能把公开模板当成完整
域名清单，必须根据目标电脑的 Mihomo 实时连接做只读发现、预览并取得授权后
写入本机覆写。

## 它解决什么

- ChatGPT 页面打不开、登录链路异常或流式输出中断；
- Codex 桌面端出现可验证网络错误；
- 普通网站仍正常，但当前出口不再能访问 OpenAI；
- 大量订阅节点中缺少按真实出口去重、稳定优先的自动切换；
- WorkBuddy（国内版）、Kimi、MiniMax 或 Mavis 在某台 Mac 上需要独立
  代理路径和容灾，但不能让它们的故障干扰 OpenAI。

Codex 暂时没有输出、一次延迟升高、一次偶发失败和 Cloudflare
浏览器挑战都不会单独触发切换。只有在 API、认证和 WebSocket 传输正常时，
精确识别到的 ChatGPT Cloudflare 挑战才记为可候选的
`browser_ambiguous`；其他软响应记为 `soft_unstable`，不能进入候选。

当自动探针只能看到 Cloudflare 挑战、但用户已经用真实浏览器验证登录结果时，
可以把结果按“出口 IP + ASN + 地区”指纹限时记录：成功默认保留 7 天，失败
默认排除 24 小时。反馈不会单独触发切换；出口指纹变化或有效期届满后会重新
评估，避免按节点名称永久拉黑。

## Provider 隔离

| Provider ID | 公开根域提示 | 默认状态 | 容灾状态 |
| --- | --- | --- | --- |
| `openai` | `openai.com`、`chatgpt.com` 及已审核静态/内容域 | 启用 | 独立组、状态、日志、冷却 |
| `workbuddy-cn` | `workbuddy.cn` | 禁用 | 启用后独立 |
| `kimi` | `kimi.com` | 禁用 | 启用后独立 |
| `minimax` | `minimaxi.com` | 禁用 | 启用后独立 |
| `mavis` | `mavislabs.ai` | 禁用 | 启用后独立 |

这些根域只用于识别产品和启动只读探测，不代表完整 API、认证、流式、文件或
CDN 清单。非 OpenAI Provider 默认禁用；启用前必须按 Agent 契约取得本机
证据和用户授权。各 Provider 只共享订阅中的真实节点目录；选中节点、健康记录、三层池、
故障计数、冷却、切换记录和日志全部隔离。

## 核心行为

- 每个已启用 Provider 默认每 10 秒检查自己的关键路径；OpenAI 使用 API、
  认证、ChatGPT 网页和 WebSocket 传输语义探针；其他 Provider 从保守根域
  探针开始，并按本机证据补充精确域名和关键传输探针；
- 两个独立关键目标在连续两轮内出现硬故障时走快速门；如果始终只有同一个目标
  失败，则至少确认 3 轮并观察满 30 秒，避免一次 API 探针抖动切断正常长连接；
- 每轮只对硬故障目标延迟 1 秒复核一次；复核恢复就不计故障，健康目标不重复
  请求；
- 第一轮孤立硬故障只复核当前路径，不启动候选深扫；达到切换门槛后才从活跃、
  温备、冷备池依次准备候选。候选必须在 3600 秒窗口内取得两次相隔至少 5 秒
  的完整路径成功，其中至少一次无需硬故障重试，且提交前必须有 60 秒内的新鲜
  结果；默认准备 2 个独立出口、最多真正试切 2 个；
- 先排除本地断网和 Mihomo 控制器不可用，避免盲切；
- 每个 Provider 的活跃、温备、冷备三层池按真实出口 IP 去重，并分散 ASN
  和地区；跨 Provider 的后台深度扫描串行执行，守护线程错峰启动；
- 候选排序先看健康、成功率、不同出口、不同 ASN、冷却和稳定性，
  最后才看延迟；
- 每次选候选前先通过实时 Mihomo 内核做一次 2 秒即时预检；通过后才选入 AI
  组，等待 3 秒并做完整在线复验；若复验靠硬故障目标重试才恢复，间隔 3 秒
  强制追加一次完整复验，追加复验必须首轮干净通过，否则立即回滚旧节点，且
  不关闭任何连接；
- 复验成功后默认采用 `preserve` 先接后断：保留切换前建立的 Provider 长连接，
  让仍健康的 Codex/ChatGPT WebSocket 自然结束；新连接自动使用新节点。可选的
  `replacement_only` 也只有在同进程、同主机的新链路替代连接已经出现后才清理
  旧连接；普通网站和其他 Provider 不受影响，并进入 60 秒观察期；
- 原节点至少冷却 5 分钟，恢复需连续成功；
- 只有活跃、温备和冷备中的独立出口均被本轮验证耗尽后，才判定全部不可用；
  同一故障期只通知一次并进入退避；
- 成功切换会显示 macOS 通知，例如
  `日本 01 → 美国 03；原因：连续两轮多信号硬故障（timeout）`。

默认 OpenAI 域名后缀只有：

```text
openai.com
chatgpt.com
oaistatic.com
oaiusercontent.com
oaistatsig.com
```

GitHub、Git、npm、Docker、Cloudflare/Google 等共享基础设施和普通网站不会
成为任何 Provider 的切换触发条件。

## 运行开销

只启用 OpenAI 时，开销与 0.1 版基本相同。每多启用一个 Provider，会增加一套
默认 10 秒一次的轻量前台探测和一份独立节点健康历史；因此不要启用实际上不
使用的 Provider。较重的隔离深度扫描在 Provider 之间串行，线程启动也会错峰，
不会让几百个节点同时高频测速。工具不启用 TUN、不修改全局组，也不常驻上传
数据。

## 前提

- macOS；
- Clash Verge Rev + Mihomo；
- 当前使用 macOS 系统代理；
- Mihomo 外部控制器使用本机 Unix socket，并设置密钥；
- [`uv`](https://docs.astral.sh/uv/)。

项目不会替用户自动开启 TUN，也不会把 External Controller 暴露到网络。

## 安装

先用官方 `skills` CLI 安装 Agent Skill：

```bash
npx --yes skills@latest add doublebearoliver-cyber/mihomo-ai-failover \
  --skill dbear-mihomo-ai-failover --agent codex --global --yes
```

如果某个客户端无法从仓库缩写发现嵌套 Skill，可直接使用规范目录地址：

```bash
npx --yes skills@latest add \
  https://github.com/doublebearoliver-cyber/mihomo-ai-failover/tree/main/plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover \
  --skill dbear-mihomo-ai-failover --agent codex --global --yes
```

Skill 只提供 Agent 指令和安全边界，不会自动安装本机 CLI/MCP 运行时。需要
真实诊断或操作这台 Mac 时，还必须安装下面的运行时。

推荐从固定版本安装独立 Python 环境：

```bash
uv tool install \
  'mihomo-ai-failover[mcp] @ git+https://github.com/doublebearoliver-cyber/mihomo-ai-failover@v0.2.2'
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

插件提供 `dbear-mihomo-ai-failover` Skill 和本地 stdio MCP。启动器优先
复用已安装的 `mihomo-ai-failover-mcp`；找不到时才通过 `uv` 从固定
`v0.2.2` 标签获取。它不会开启 TCP 监听。

## Claude Code 插件

```bash
claude plugin marketplace add doublebearoliver-cyber/mihomo-ai-failover
claude plugin install mihomo-ai-failover@mihomo-ai-failover
```

Codex 和 Claude 共用同一套安全工作流与 20 个 MCP 工具。写工具默认禁用；
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
| `providers-list` | 否 | 查看公开模板和本机启用状态 |
| `provider-check --provider ID` | 否 | 对比直连和当前系统代理路径；不切换 |
| `provider-observe --provider ID` | 否 | 观察脱敏后的本机连接域名 |
| `provider-overlay-preview --provider ID` | 否 | 预览个人版私有覆写 |
| `provider-overlay-apply --provider ID` | 是 | 写入经授权的私有覆写；不直接改 Clash |

Provider 相关命令以及 `status`、`check`、`inventory`、`run-once` 支持
`--provider ID`；所有命令支持 `--config /绝对路径/config.yaml`。

### 让 Agent 适配一个新 Provider

下面以 Kimi 为例。发现阶段保持只读，并要求用户在观察窗口内实际打开、登录、
发起对话或生成：

```bash
mihomo-ai-failover diagnose
mihomo-ai-failover providers-list
mihomo-ai-failover provider-check --provider kimi
mihomo-ai-failover provider-observe --provider kimi --duration-seconds 20
```

只有进程关联或其他可复核证据充分的域名，才应作为精确域名进入预览；仅仅在
浏览器观察窗口中同时出现的域名属于 `temporal_only`，不会自动推荐。共享
CDN、登录平台或统计域名不能作为关键故障触发器。
如果直连本来稳定可用而代理路径更差或没有必要，不要强行启用该 Provider
容灾；保留原有直连/规则行为。

```bash
mihomo-ai-failover provider-overlay-preview \
  --provider kimi \
  --domain api.example.invalid \
  --critical-domain stream.example.invalid \
  --enable
```

上面 `.invalid` 只是格式示例，不能原样使用。Agent 必须替换为这台 Mac 上
已审核的真实域名。用户确认预览后才允许写入：

```bash
mihomo-ai-failover provider-overlay-apply \
  --provider kimi \
  --domain '<已审核精确域名>' \
  --critical-domain '<已审核关键域名>' \
  --enable \
  --confirm APPLY_PROVIDER_OVERLAY
```

覆写本身不会改 Clash。已有安装应先停止监控，再运行 `profile-preview` 和
`profile-install --confirm APPLY_PROFILE_INTEGRATION`，按结果重启 Clash Verge，
然后执行 `check --provider kimi`、`inventory --provider kimi` 和
`service-start`。完整 Agent 工作流见
[Skill 的 Provider 适配参考](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/references/provider-adaptation.md)。

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
4. 为每个已启用 Provider 添加一个独立 `select` 组；
5. 只把该 Provider 的公开根域和本机已审核精确域名放入 Rules enhancement；
6. 每次写入前创建带清单和 SHA-256 的备份；
7. 原子写入 enhancement，最后才写 `profiles.yaml`。

因此更新订阅、重启 Clash Verge 和重启 Mac 后仍可保留。路径穿越、
symlink 逃逸、冲突规则和写入期间文件变化都会让安装安全停止。
`--disable` 会在监控服务重启后停止该 Provider 状态机，但为了不擅自删除用户
规则，已安装的持久化组/规则不会自动移除；需要经授权回滚或定向清理。

## 本地文件

默认路径：

- 配置：`~/Library/Application Support/Mihomo AI Failover/config.yaml`
- 私有 Provider 覆写：
  `~/Library/Application Support/Mihomo AI Failover/providers.local.yaml`
- 状态：`~/Library/Application Support/Mihomo AI Failover/state.json`
- 非 OpenAI Provider 状态：
  `~/Library/Application Support/Mihomo AI Failover/providers/<id>/state.json`
- 备份：`~/Library/Application Support/Mihomo AI Failover/backups/`
- 日志：`~/Library/Logs/Mihomo AI Failover/monitor.jsonl`
- 非 OpenAI Provider 日志：
  `~/Library/Logs/Mihomo AI Failover/providers/<id>/monitor.jsonl`
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
- 每个 Provider 只控制自己的组；不会用一个 Provider 的故障触发另一个；
- 只在同一台 Mac 上运行，不提供远程控制端口；
- ChatGPT 网页中的云端模型不能直接访问本机 `localhost`；
- 0.x 版不提供远程到本地桥接，不以公网控制器代替该能力；
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
