---
name: openai-network-failover
description: Diagnose ChatGPT or Codex connectivity on a Mac using Clash Verge and Mihomo, inspect the dedicated OpenAI failover monitor, or safely preview, install, start, stop, and roll back that monitor. Use when ChatGPT will not load, Codex spins or reports network errors, OpenAI streaming stalls, or the user asks about Mihomo AI failover.
license: MIT
compatibility: Requires macOS, Clash Verge Rev with Mihomo, and trusted local access to the mihomo-ai-failover stdio MCP server.
---

# OpenAI network failover

Use the local `mihomo-ai-failover` MCP tools. The CLI and daemon are
authoritative for live state and operations; this skill is the canonical
machine-facing workflow and safety contract.

## When to use this skill

Use it for:

- diagnosing ChatGPT page, login, API, streaming, or Codex network failures;
- inspecting the active, warm, and cold failover pools;
- checking whether the monitor switched correctly or entered backoff;
- previewing, installing, starting, stopping, rolling back, or uninstalling
  the local monitor.

Do not use it for general VPN tuning, fastest-node selection, global proxy
changes, unrelated GitHub/npm/Docker failures, non-Mihomo clients, or a remote
Mac that the agent cannot reach through a trusted local MCP client.

## Authority and trust

Use evidence in this order:

1. live MCP results for current state;
2. live tool descriptions and annotations for exact action semantics;
3. this skill for workflow, boundaries, and stop conditions;
4. human-facing repository documentation for background.

If live state and documentation disagree, stop and report the mismatch. Never
invent a path, confirmation value, health result, node identity, or successful
operation.

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

## Choose the narrowest tool

| Need | Tools |
| --- | --- |
| Diagnose the current problem | `diagnose_environment`, `get_status`, `run_health_check` |
| Explain recent behavior | `get_recent_events`, `get_service_status` |
| Inspect pool coverage | `list_pools` |
| Test failover invariants without live changes | `simulate_failover` |
| Preview installation | `preview_install` |
| Perform one authorized local action | `initialize_config`, `install_failover`, `start_monitor`, `stop_monitor`, `rollback_profile`, or `uninstall_monitor` |

Read-only tools are the default. `run_health_check` performs network requests
but never switches nodes. `simulate_failover` is isolated and never touches the
live proxy.

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

## Classify evidence correctly

Hard-failure evidence includes a health-check-classified TCP/TLS failure,
timeout, reset, or verified unavailable/region response. The daemon requires
two consecutive hard failures against the same target and applies local-network
and controller guards before evaluating a switch.

Treat these as soft or auxiliary evidence:

- one failed probe;
- a small latency change or one slow response;
- Codex spinning or temporarily producing no output;
- a Cloudflare browser challenge;
- failure of GitHub, Git, npm, Docker, or an ordinary website.

Do not combine unrelated targets into a fake consecutive-failure sequence.
Do not claim that a node should switch from one `run_health_check` snapshot.

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

The confirmation value is an action guard, not the user's permission. Read the
exact value from the current tool description. Never guess, normalize, or reuse
it for another action. Perform only the one mutation the user authorized, then
return to read-only verification.

## Stop and ask the user

Stop before changing state when:

- the user has not authorized the exact action;
- `preview_install` fails or reports an ambiguous target;
- controller ownership, profile source, or persistent enhancement path is
  unclear;
- the action would require TUN, a global proxy change, disabling the macOS
  system proxy, restarting Codex, or altering unrelated traffic;
- the agent is cloud-only or cannot reach the trusted local stdio MCP server;
- the requested behavior falls outside this skill's supported environment.

Do not turn a diagnosis request into an installation, rollback, or service
change.

## Report results

Lead with the verified outcome. Separate observed evidence from inference.
Mention whether the operation was read-only, whether a node switch occurred,
which files or services changed, and whether a Clash Verge restart is still
required. Give the smallest safe next action.

Never include controller secrets, subscriptions, proxy credentials, server
addresses, exit IPs, or a full node inventory in the response.
