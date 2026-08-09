---
name: mihomo-ai-failover
description: Diagnose and safely operate automatic proxy failover for ChatGPT, Codex, WorkBuddy (China), Kimi, MiniMax, and Mavis on macOS with Clash Verge Rev and Mihomo. Use when AI login fails, ChatGPT will not load, Codex spins or reports network errors, streaming stalls, or the user needs verified hard-failure detection, stable node switching, Provider-specific routing, local domain discovery, installation, rollback, or recovery.
---

# Mihomo AI Provider failover

Use the local `mihomo-ai-failover` MCP tools. The CLI and daemon are
authoritative for live state and operations; this skill is the canonical
machine-facing workflow and safety contract.

OpenAI is enabled by default. Every other public Provider template is disabled
until local evidence has been reviewed and the user authorizes a private
overlay and persistent profile update.

## When to use this skill

Use it for:

- diagnosing ChatGPT page, login, API, streaming, or Codex network failures;
- diagnosing WorkBuddy (China), Kimi, MiniMax, or Mavis network paths;
- discovering sanitized Provider hostnames on the user's Mac and previewing a
  narrow, private adaptation;
- inspecting the active, warm, and cold failover pools;
- checking whether the monitor switched correctly or entered backoff;
- previewing, installing, starting, stopping, rolling back, or uninstalling
  the local monitor.

Do not use it for general VPN tuning, fastest-node selection, global proxy
changes, unrelated GitHub/npm/Docker failures, arbitrary website monitoring,
non-Mihomo clients, or a remote Mac that the agent cannot reach through a
trusted local MCP client.

## Check runtime access before tools

This Skill is an operating contract, not the failover runtime itself. Before
following a tool workflow, check whether the trusted local
`mihomo-ai-failover` stdio MCP server and its tools are actually available.

- If the MCP tools are available, start with the read-only workflow below.
- If the tools are unavailable, do not claim that this Mac was diagnosed and
  do not silently install anything. Report the missing local dependency and
  direct the user to the repository root README for the pinned runtime or
  plugin installation.
- A cloud-only model cannot reach the user's Mihomo controller. It may explain
  the workflow, but it must stop before claiming live state or making changes.

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
- Keep Provider state isolated. A WorkBuddy, Kimi, MiniMax, or Mavis failure
  must not move the OpenAI group or increment OpenAI failure counters, and vice
  versa.
- Keep GitHub, Git, npm, Docker, shared infrastructure, and ordinary websites
  out of every Provider failover trigger.
- Never guess a login, API, WebSocket, CDN, storage, or authentication domain.
  Public roots are bootstrap hints, not a complete domain claim.
- Do not expose controller secrets, subscription URLs, proxy credentials,
  server addresses, exit IPs, or a full node inventory.
- Leave `include_node_names` false unless the user explicitly needs node names.
- MCP mutations are disabled by default. Do not ask the user to enable them
  until a read-only diagnosis and preview show that a mutation is necessary.

## Choose the narrowest tool

| Need | Tools |
| --- | --- |
| Inspect available Providers | `list_provider_profiles` |
| Compare a Provider's direct and system-proxy paths | `check_provider_paths` |
| Observe sanitized local Provider hostnames | `discover_provider_domains` |
| Preview a private Provider adaptation | `preview_provider_overlay` |
| Write one authorized private adaptation | `apply_provider_overlay` |
| Diagnose the current problem | `diagnose_environment`, `get_status`, `run_health_check` |
| Explain recent behavior | `get_recent_events`, `get_service_status` |
| Inspect pool coverage | `list_pools` |
| Test failover invariants without live changes | `simulate_failover` |
| Preview installation | `preview_install` |
| Record a user-verified real-browser result | `stop_monitor`, then `record_web_feedback`, then `start_monitor` |
| Perform one authorized local action | `initialize_config`, `install_failover`, `start_monitor`, `stop_monitor`, `record_web_feedback`, `rollback_profile`, or `uninstall_monitor` |

Read-only tools are the default. `run_health_check` performs network requests
but never switches nodes. `simulate_failover` is isolated and never touches the
live proxy.

For Provider-specific tools, always pass the explicit `provider_id`. Read
[`references/provider-adaptation.md`](references/provider-adaptation.md) before
discovering or enabling a non-OpenAI Provider. Read
[`references/public-profiles.md`](references/public-profiles.md) when selecting
a built-in profile or interpreting its bootstrap probe.

## Diagnose a current failure

1. Call `diagnose_environment`.
2. Call `list_provider_profiles` and identify the exact Provider.
3. Call `get_status` and `run_health_check` with that `provider_id`.
4. If the failure appears intermittent, compare up to 20 sanitized records with
   `get_recent_events`.
5. Explain which layer failed:
   - local network or DNS;
   - Mihomo controller or dedicated Provider group;
   - that Provider's required probes and dedicated proxy group;
   - browser or app symptom without a verified network failure.

Do not call an installation or service mutation during diagnosis.

## Classify evidence correctly

Hard-failure evidence includes a health-check-classified TCP/TLS failure,
timeout, reset, or verified unavailable/region response. The daemon applies
local-network and controller guards before evaluating a switch. Two guarded
rounds open the fast gate only when at least two distinct critical targets fail
across them. A single repeatedly failing target requires at least three rounds
and 30 seconds of observation. The first hard-failure round confirms only the
selected route; it does not start candidate deep scans or switch the live group.
Within each round, a hard-failing target is retried once; a successful retry
means that round does not count as hard for that target.

Treat these as soft or auxiliary evidence:

- one failed probe;
- a small latency change or one slow response;
- Codex spinning or temporarily producing no output;
- a Cloudflare browser challenge;
- failure of GitHub, Git, npm, Docker, or an ordinary website.

An explicit user report that a Provider login succeeded or failed in the real
browser/app may be recorded with `record_web_feedback`, but never infer that result
from an automated Cloudflare challenge, a timeout in a browser-control tool, or
Codex behavior. Browser feedback is time-limited, bound to the observed exit
IP + ASN + country fingerprint, and never triggers a switch by itself. Stop the
monitor before recording it, then restart the monitor and return to read-only
verification.

Do not claim that a node should switch from one `run_health_check` snapshot.

## Inspect failover behavior

- Use `list_pools` for active, warm, and cold pool counts and independent-exit
  coverage.
- Use `simulate_failover` to verify the adaptive failure gates, prepared-route
  timing, connection-preservation mode, and different-exit preference without
  touching the live proxy.
- Use `get_recent_events` to distinguish a hard failure, a soft anomaly, a
  successful switch, cooldown, and an all-unavailable backoff episode.
- Treat a light delay result as preflight only. A switch candidate needs two
  fresh full Provider-path samples, a just-in-time live-core preflight,
  live-route verification before commit, and a post-switch probation period.
  The deep evidence needs two usable samples with at least one retry-free result.
  A retry-assisted live acceptance must pass a mandatory retry-free follow-up
  after three seconds before a newly selected route can commit.
- Default connection draining is `preserve`: never interpret an old-chain
  Codex/ChatGPT WebSocket as stale merely because the group changed. Optional
  cleanup requires a newer same-process replacement on the new route.

## Adapt a Provider to this Mac

Use the detailed workflow in
[`references/provider-adaptation.md`](references/provider-adaptation.md). The
required order is:

1. Diagnose and list profiles; do not mutate.
2. Call `check_provider_paths` for the target Provider.
   If direct is healthy and proxying is unnecessary or worse, do not force the
   Provider into this system.
3. Ask the user to actively exercise only that Provider during a bounded
   `discover_provider_domains` window.
4. Treat known roots as confirmation, process-correlated hosts as candidates,
   and temporal-only browser hosts as unproven. Shared infrastructure is never
   a failover trigger.
5. Corroborate every new exact domain. Mark a domain critical only when a
   failed TCP/TLS/HTTP transport path is an objective hard signal for that
   Provider. A transport probe does not prove login, account, or model-output
   success.
6. Call `preview_provider_overlay`, explain exact domains, critical probes,
   group, files, restart, and resource impact, then obtain authorization.
7. Only then call `apply_provider_overlay` with local mutation opt-in and the
   exact confirmation shown by the tool.
8. Preview/apply persistent profile integration, restart Clash Verge if
   required, run Provider-specific health/inventory checks, and only then start
   or restart the monitor.

The private overlay is local machine state. Never paste its observed domains
into source templates, tests, commits, issues, or model prompts for other
machines.

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
- `apply_provider_overlay`
- `install_failover`
- `start_monitor`
- `stop_monitor`
- `record_web_feedback`
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
- a non-OpenAI Provider has only a public bootstrap root but no local evidence
  for the user's real API/auth/streaming path;
- enabling a Provider would require reusing another Provider's group or using
  a shared CDN/identity platform as a hard-failure trigger.

Do not turn a diagnosis request into an installation, rollback, or service
change.

## Report results

Lead with the verified outcome. Separate observed evidence from inference.
Mention the Provider, whether the operation was read-only, whether a node
switch occurred, which files or services changed, and whether a Clash Verge
restart is still required. Give the smallest safe next action.

Never include controller secrets, subscriptions, proxy credentials, server
addresses, exit IPs, or a full node inventory in the response.
