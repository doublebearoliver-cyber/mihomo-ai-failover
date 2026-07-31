---
name: openai-network-failover
description: Diagnose ChatGPT or Codex connectivity on a Mac using Clash Verge and Mihomo, inspect the dedicated OpenAI failover monitor, or safely preview, install, start, stop, and roll back that monitor. Use when ChatGPT will not load, Codex spins or reports network errors, OpenAI streaming stalls, or the user asks about Mihomo AI failover.
---

# OpenAI network failover

Use the local `mihomo-ai-failover` MCP tools. The CLI and daemon are
authoritative; this skill only guides safe use.

## Non-negotiable boundaries

- Diagnose before changing anything.
- Never treat a silent or spinning Codex UI as independent proof that a proxy
  node failed.
- Never switch because another node is a few milliseconds faster.
- Never modify Mihomo's generated runtime YAML.
- Never enable TUN, disable the macOS system proxy, restart Codex, or change a
  global proxy group.
- Keep GitHub, Git, npm, Docker, and ordinary websites out of the OpenAI
  failover trigger.
- Do not expose controller secrets, subscription URLs, proxy credentials,
  server addresses, exit IPs, or a full node inventory.
- Leave `include_node_names` false unless the user explicitly needs node names.
- MCP mutations are disabled by default. Do not ask the user to enable them
  until a read-only diagnosis and preview show that a mutation is necessary.

## Diagnose a current failure

1. Call `diagnose_environment`.
2. Call `get_status`.
3. Call `run_health_check`.
4. If the failure appears intermittent, compare up to 20 sanitized records with
   `get_recent_events`.
5. Explain which layer failed:
   - local network or DNS;
   - Mihomo controller or dedicated AI group;
   - OpenAI API, authentication, or ChatGPT web path;
   - browser or app symptom without a verified network failure.

Do not call an installation or service mutation during diagnosis.

## Inspect failover behavior

- Use `list_pools` for active, warm, and cold pool counts and independent-exit
  coverage.
- Use `simulate_failover` to verify the two-hard-failure timing and
  different-exit preference without touching the live proxy.
- Use `get_recent_events` to distinguish a hard failure, a soft anomaly, a
  successful switch, cooldown, and an all-unavailable backoff episode.

## Install or change the local monitor

1. Call `preview_install`.
2. State the exact local files and services that would change, including
   whether Clash Verge must be restarted.
3. Ask the user to explicitly authorize the change.
4. Only after authorization, tell the user that local mutation opt-in is
   required (`MIHOMO_AI_FAILOVER_MCP_MUTATIONS=1` or
   `mcp_allow_mutations: true`).
5. Call the relevant mutation with its exact confirmation token. Never invent
   or normalize confirmation text.
6. Re-run `diagnose_environment` and `get_service_status`.

Use these tools only for the action the user approved:

- `initialize_config`
- `install_failover`
- `start_monitor`
- `stop_monitor`
- `rollback_profile`
- `uninstall_monitor`

## Report results

Lead with the verified outcome. Separate observed evidence from inference.
Mention whether the operation was read-only, whether a node switch occurred,
and whether a Clash Verge restart is still required.
