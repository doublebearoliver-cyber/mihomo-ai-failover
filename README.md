# Mihomo AI Failover

OpenAI-aware failover for Mihomo on macOS. It keeps a working selected node and
switches a dedicated AI proxy group only after two consecutive, verified
hard-failure rounds on the real OpenAI path.

> Version `0.1.0` is an early public preview for macOS, Clash Verge Rev, and
> Mihomo. It keeps the macOS system proxy and never enables TUN automatically.

[中文说明](README.zh-CN.md) ·
[AI agent contract](plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md) ·
[Agent integration](docs/agent-integration.md) ·
[Architecture](docs/architecture.md) ·
[Validation](docs/validation.md)

> **AI agents: read the
> [`openai-network-failover` skill](plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md)
> before using any MCP tool.** It is the canonical machine-facing contract for
> supported environments, safety boundaries, tool order, stop conditions, and
> result reporting. This README is not a substitute for that contract.

## Why

Generic `url-test` groups optimize latency. They do not prove that ChatGPT
login, the OpenAI API, streaming connections, or a particular exit region are
usable. This project validates the real OpenAI path and:

- ignores small latency changes and isolated soft anomalies;
- retries only the hard-failing probe once before a round can count as a
  verified hard failure;
- excludes local-network and controller failures from blind switching;
- maintains active, warm, and cold pools deduplicated by observed exit IP;
- starts isolated candidate validation after the first hard-failure round and
  runs it in parallel with the second confirmation round, while requiring two
  fresh usable full-path samples, at least one retry-free, before a candidate
  can be selected; three independent candidates are prepared while at most two
  are live-selected; commit reuses already prepared candidates instead of
  blocking to refill all three, and a failed just-in-time preflight does not
  consume that live-selection budget;
- ranks health, success history, exit/ASN diversity, cooldown, and stability
  before latency;
- verifies the selected candidate on the live route before committing; a
  retry-assisted pass requires a second, retry-free verification after three
  seconds, otherwise it rolls back, followed by a 60-second probation period;
- reruns a just-in-time live-core preflight immediately before each candidate
  selection, so stale preparation evidence cannot cause a blind switch;
- closes stale OpenAI connections across every old chain only after the new
  route verifies;
- notifies once per all-unavailable outage episode and backs off;
- exposes the same behavior through a CLI, local stdio MCP, Codex plugin, and
  Claude Code plugin.

A silent or spinning Codex UI is only an auxiliary symptom. It never triggers
a switch by itself. GitHub, Git, npm, Docker, and ordinary websites are outside
the OpenAI failure trigger.

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

Install [`uv`](https://docs.astral.sh/uv/), then:

```bash
uv tool install \
  'mihomo-ai-failover[mcp] @ git+https://github.com/doublebearoliver-cyber/mihomo-ai-failover@v0.1.0'
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

Both plugins bundle the same `openai-network-failover` skill and local MCP
server. MCP mutations are disabled by default and require both local opt-in and
an exact server-enforced confirmation.

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

- Controls only the dedicated AI group.
- Uses the local Unix-domain Mihomo controller by default.
- Reads the controller secret at runtime and never returns it.
- Does not store subscription URLs, proxy server addresses, or proxy
  credentials.
- MCP never returns exit IPs and hides node names by default.
- Does not expose a TCP control listener.

A hosted model cannot directly reach a user's localhost. An authenticated
remote-to-local bridge is intentionally outside version 0.1. See
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
