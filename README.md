# Mihomo AI Failover — automatic proxy failover for ChatGPT and Codex

Automatic, stability-first AI proxy failover for macOS with Clash Verge Rev
and Mihomo. It diagnoses ChatGPT login/loading failures, Codex network errors,
and stalled AI streams, keeps a working selected node, and switches only the
affected Provider's dedicated proxy group after guarded, verified hard-failure
evidence on that Provider's real path.

> Version `0.2.2` is an early public preview for macOS, Clash Verge Rev, and
> Mihomo. OpenAI is enabled by default. WorkBuddy (China), Kimi, MiniMax, and
> Mavis remain disabled until their real local traffic is observed and
> reviewed. The project never enables TUN automatically.

[中文说明](README.zh-CN.md) ·
[AI agent contract](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/SKILL.md) ·
[Agent integration](docs/agent-integration.md) ·
[Architecture](docs/architecture.md) ·
[Validation](docs/validation.md)

> **AI agents: read the
> [`dbear-mihomo-ai-failover` skill](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/SKILL.md)
> before using any MCP tool.** It is the canonical machine-facing contract for
> supported environments, safety boundaries, tool order, stop conditions, and
> result reporting. This README is not a substitute for that contract.

## Public core and private adaptation

There are two layers, not two long-lived forks:

- the public layer contains the engine, conservative Provider templates,
  CLI/MCP/Skill contracts, installer, rollback, and tests;
- the personal layer is a mode-`0600` local `providers.local.yaml` overlay with
  only that Mac's approved Provider enablement, exact domains, and probes.

The public package contains no local nodes, exits, or observed hostnames. An
agent must use local Mihomo evidence, preview the narrow overlay, and obtain
authorization before writing it.

## Why

Generic `url-test` groups optimize latency. They do not prove that ChatGPT
login, the OpenAI API, streaming connections, or a particular exit region are
usable. This project validates each enabled Provider path and:

- ignores small latency changes and isolated soft anomalies;
- retries only the hard-failing probe once before a round can count as a
  verified hard failure;
- switches after two guarded rounds only when at least two independent critical
  targets failed across those rounds; one repeatedly failing target requires at
  least three rounds and a 30-second observation window;
- excludes local-network and controller failures from blind switching;
- maintains active, warm, and cold pools deduplicated by observed exit IP;
- confirms the selected route before starting isolated candidate validation,
  so an isolated first failure cannot launch a broad node scan; two independent
  candidates are prepared while at most two are live-selected, and each still
  needs two usable full-path samples with at least one retry-free result;
- ranks health, success history, exit/ASN diversity, cooldown, and stability
  before latency;
- verifies the selected candidate on the live route before committing; a
  retry-assisted pass requires a second, retry-free verification after three
  seconds, otherwise it rolls back, followed by a 60-second probation period;
- reruns a just-in-time live-core preflight immediately before each candidate
  selection, so stale preparation evidence cannot cause a blind switch;
- uses make-before-break connection draining: the default `preserve` mode does
  not delete old Provider connections after a switch, so an active Codex or
  ChatGPT WebSocket can finish naturally; optional `replacement_only` cleanup
  requires a newer same-process replacement on the new route;
- notifies once per all-unavailable outage episode and backs off;
- exposes the same behavior through a CLI, local stdio MCP, Codex plugin, and
  Claude Code plugin.

A silent or spinning AI client is only an auxiliary symptom. It never triggers
a switch by itself. GitHub, Git, npm, Docker, shared infrastructure, and
ordinary websites are outside every Provider failure trigger.

Each Provider has a separate select group, health history, active/warm/cold
pools, cooldowns, switch episode, state file, and log. Providers share only the
read-only subscription node catalog. Background deep scans are serialized and
daemon starts are staggered to bound local load.

| Provider ID | Conservative public root | Default |
| --- | --- | --- |
| `openai` | reviewed OpenAI/ChatGPT roots | Enabled |
| `workbuddy-cn` | `workbuddy.cn` | Disabled |
| `kimi` | `kimi.com` | Disabled |
| `minimax` | `minimaxi.com` | Disabled |
| `mavis` | `mavislabs.ai` | Disabled |

These are bootstrap identities, not exhaustive API/auth/streaming/CDN lists.
Non-OpenAI profiles must be adapted from evidence on the target Mac before
automatic failover is enabled.

Each enabled Provider adds a bounded ten-second foreground check and its own
health history. Enable only Providers the operator uses. Heavier isolated deep
scans are serialized across Providers, so hundreds of subscription nodes are
not all tested at high frequency.

An exact ChatGPT Cloudflare challenge is classified as `browser_ambiguous`, not
as a generic healthy response. It can remain candidate-eligible only when the
API, authentication, and WebSocket transport probes are healthy. Other soft
responses are `soft_unstable` and cannot become candidates. A user-confirmed
real-browser result can also be stored against the observed exit IP + ASN +
country fingerprint. Confirmed results last seven days by default; rejected
results exclude that exit for 24 hours. Feedback never triggers a switch by
itself and automatically becomes inapplicable when the exit fingerprint
changes.

## Install

Install the Agent Skill with the official `skills` CLI:

```bash
npx --yes skills@latest add doublebearoliver-cyber/mihomo-ai-failover \
  --skill dbear-mihomo-ai-failover --agent codex --global --yes
```

If a client cannot discover a nested Skill from the repository shorthand, use
the canonical Skill directory directly:

```bash
npx --yes skills@latest add \
  https://github.com/doublebearoliver-cyber/mihomo-ai-failover/tree/main/plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover \
  --skill dbear-mihomo-ai-failover --agent codex --global --yes
```

The Skill supplies instructions and safety boundaries; it does not install the
local CLI/MCP runtime. To diagnose or operate a Mac, install the runtime too.

Install [`uv`](https://docs.astral.sh/uv/), then:

```bash
uv tool install \
  'mihomo-ai-failover[mcp] @ git+https://github.com/doublebearoliver-cyber/mihomo-ai-failover@v0.2.2'
```

Diagnose and preview before writing:

```bash
mihomo-ai-failover diagnose
mihomo-ai-failover check
mihomo-ai-failover profile-preview
```

Apply the persistent Clash Verge enhancements and install the user
LaunchAgent:

```bash
mihomo-ai-failover install \
  --confirm INSTALL_MIHOMO_AI_FAILOVER
```

If `restart_required` is true, restart Clash Verge, then:

```bash
mihomo-ai-failover check
mihomo-ai-failover inventory
mihomo-ai-failover service-start
```

The installer never edits generated `clash-verge.yaml`. It backs up and updates
the selected profile's persistent Groups and Rules enhancements.

## Codex plugin

```bash
codex plugin marketplace add doublebearoliver-cyber/mihomo-ai-failover
codex plugin add mihomo-ai-failover@mihomo-ai-failover
```

## Claude Code plugin

```bash
claude plugin marketplace add doublebearoliver-cyber/mihomo-ai-failover
claude plugin install mihomo-ai-failover@mihomo-ai-failover
```

Both plugins bundle the same `mihomo-ai-failover` Skill and local MCP server.
MCP mutations are disabled by default and require both local opt-in and an
exact server-enforced confirmation.

Agents without native plugin support can use the generic stdio MCP definition
and load the same `SKILL.md` as instructions. The skill does not grant access to
the Mac: the agent still needs a trusted local MCP client. See
[Agent integration](docs/agent-integration.md).

To record an explicitly verified browser result, stop the monitor first so the
state lock is uncontended:

```bash
mihomo-ai-failover service-stop
mihomo-ai-failover web-feedback \
  --node 'local node display name' \
  --status confirmed \
  --reason browser_login_success \
  --confirm RECORD_WEB_FEEDBACK
mihomo-ai-failover service-start
```

Use `rejected` and `browser_login_failed` for a verified failure. The command
refuses to record feedback without an observed exit fingerprint.

## Adapt another Provider

Ask the user to exercise one Provider while the read-only observer runs:

```bash
mihomo-ai-failover diagnose
mihomo-ai-failover providers-list
mihomo-ai-failover provider-check --provider kimi
mihomo-ai-failover provider-observe --provider kimi --duration-seconds 20
```

Known roots confirm coverage. Process-correlated hosts may be proposed as
exact domains. Browser-only `temporal_only` hosts are never auto-added, and
shared infrastructure is never a critical failover trigger. Preview before
writing:

```bash
mihomo-ai-failover provider-overlay-preview \
  --provider kimi \
  --domain '<reviewed exact hostname>' \
  --critical-domain '<reviewed critical hostname>' \
  --enable
```

If direct access is already stable and proxying is unnecessary or worse, do
not force that Provider into a failover group.

After explicit authorization, write with
`--confirm APPLY_PROVIDER_OVERLAY`, then separately preview/apply the
persistent profile integration and restart Clash Verge when requested. See the
[Provider adaptation contract](plugins/mihomo-ai-failover/skills/dbear-mihomo-ai-failover/references/provider-adaptation.md).

Disabling a Provider stops its state machine after the service restarts. To
avoid deleting user-managed rules, previously installed persistent rules are
removed only through an authorized rollback or explicit profile cleanup.

## Roll back

```bash
mihomo-ai-failover service-stop
mihomo-ai-failover profile-rollback \
  --confirm ROLLBACK_PROFILE_INTEGRATION
mihomo-ai-failover service-uninstall \
  --confirm UNINSTALL_LAUNCH_AGENT
```

Restart Clash Verge after restoring the enhancement backup. The LaunchAgent
plist is moved to Trash rather than permanently deleted.

## Safety and privacy

- Controls only dedicated Provider groups; one Provider cannot trigger another.
- Uses the local Unix-domain Mihomo controller by default.
- Reads the controller secret at runtime and never returns it.
- Does not store subscription URLs, proxy server addresses, or proxy
  credentials.
- MCP never returns exit IPs and hides node names by default.
- Does not expose a TCP control listener.

A hosted model cannot directly reach a user's localhost. An authenticated
remote-to-local bridge is intentionally outside version 0.x. See
[PRIVACY.md](PRIVACY.md) for exact network destinations and local data.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/scan_sensitive.py
uv run python -m build
```

## License

MIT. See [LICENSE](LICENSE).
