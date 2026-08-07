# Architecture

## Trust boundary

The daemon, CLI, and MCP server run as the current macOS user. The project uses
Mihomo's local Unix-domain controller socket and reads its secret from the
generated local Clash Verge config at runtime. No project component exposes a
remote listener.

The CLI and daemon are authoritative. Agent skills and MCP tools are adapters;
the failover policy does not depend on a model being available.

```mermaid
flowchart LR
    UI["ChatGPT / Codex"] --> SP["macOS system proxy"]
    SP --> AI["Dedicated AI select group"]
    AI --> Node["Selected Mihomo node"]
    Watch["Local failover daemon"] --> Socket["Mihomo Unix socket"]
    Socket --> AI
    Watch --> Probe["OpenAI API + auth + web probes"]
    Watch --> State["Local state and JSONL log"]
    Agent["Codex / Claude"] --> MCP["stdio MCP"]
    MCP --> Watch
```

## Configuration sources

The generated `clash-verge.yaml` is read only to discover the mixed proxy port,
Unix controller socket, controller secret, DNS configuration, and live proxy
definitions used by the isolated scanner.

Persistent routing changes go through the selected profile's Clash Verge
Groups and Rules enhancement files referenced by `profiles.yaml`. Existing
enhancements are preserved. Writes are backed up, hash-checked, atomic, and
ordered so `profiles.yaml` is written last.

## Health state machine

1. Probe OpenAI API, OpenAI authentication, ChatGPT web, and ChatGPT
   WebSocket transport concurrently.
2. A valid API `401` JSON authentication response and valid auth OIDC issuer
   prove the required path.
3. Timeouts, TCP/TLS/DNS failures, resets, unsupported-region responses, or
   invalid required responses are hard failures.
4. Rate limits, transient upstream 5xx responses, access responses, and
   Cloudflare browser challenges are soft. An exact ChatGPT challenge is
   `browser_ambiguous` and remains candidate-eligible only when API,
   authentication, and WebSocket transport are all healthy. Other soft
   combinations are `soft_unstable` and are not candidates.
5. Before counting a hard failure, direct probes check whether the local
   network itself is down.
6. Within a round, only probes classified hard are retried once after one
   second. A recovered retry makes that target non-hard; healthy and soft
   probes are not repeated.
7. Two aggregate hard-failure rounds, separated by at least eight seconds, are
   required. The failing critical target may differ between rounds because the
   decision represents route failure, not a target-specific counter.
8. The first hard-failure round immediately starts isolated candidate
   preparation. A successful, soft-only, direct-network-down, or
   controller-down round resets confirmation and cannot trigger switching.

Cloudflare challenges remain soft because command-line probes cannot prove the
interactive browser outcome. An explicitly user-verified login result can be
stored separately as browser feedback. It is bound to the observed exit IP,
ASN, and country, expires after a configured TTL, and affects candidate
eligibility/ranking only; it never increments a hard-failure streak or triggers
a switch.

Candidate preparation runs in parallel with the eight-second failure
confirmation gap. For already prepared independent candidates whose live
acceptance is retry-free, the modeled upper bound is 19 seconds from the first
verified failure for candidate one and 30 seconds for candidate two, including
the just-in-time preflight, connection wait, and live verification. A mandatory
reverification for an unstable-looking candidate, or a cold rescue scan, is
deliberately not promised this latency bound.

## Candidate pools

- Active: target 12-16 recently verified independent exits; a rotating batch of
  four gets light preflight every 10 seconds and two get a full-path scan every
  2 minutes.
- Warm: target 30-40 previously verified independent exits; rotating
  full-path batches of two every 5 minutes.
- Cold: remaining real proxy nodes; rotating full-path batches of four every
  6 hours.
- Refill: when active or warm is below target, two cold candidates are scanned
  every 60 seconds instead of waiting for the normal cold cycle.

All maintenance scans pause during hard-failure confirmation, switching, and
the 60-second probation period. This prevents inventory work from competing
with the foreground health decision.

Pool size is constrained by available independent exits. Metadata, quota
notices, `DIRECT`, `REJECT`, and proxy groups are excluded. Deep scans launch a
temporary local-only Mihomo with an in-memory subset of proxy definitions, so
the live AI group does not move during inventory work.

Candidates are ranked by full-path eligibility, recent success rate, distinct
exit IP, ASN diversity, cooldown/recovery state, stability, and only then
median latency. Duplicate exit IPs are removed from each attempt order. A
candidate requires two eligible full-path samples in a 120-second window,
separated by at least five seconds, with the newest sample no older than 30
seconds. A light Mihomo delay probe alone never clears recovery or proves
candidate eligibility.
An unexpired rejected browser fingerprint is excluded from all three failover
layers. A confirmed fingerprint is preferred among otherwise healthy,
independent candidates. Changing the observed fingerprint immediately makes
the old feedback inapplicable, so a dynamic exit can be evaluated again.

## Switching

The daemon prepares bounded candidates by active, warm, then cold layer without
moving the live AI group. Immediately before selection, it reruns a two-second
Mihomo live-core preflight; stale candidates are quarantined without touching
the group. After selecting one that passes, it waits three seconds and repeats
the real OpenAI route probes through the live group. Two usable candidate
samples are required and at least one must be retry-free. If the live acceptance
round recovers a hard target on retry, the daemon waits three seconds and runs a
mandatory second round. That second round must pass retry-free; otherwise the
candidate is quarantined and the old selection is immediately restored without
closing connections. A clean first round or clean mandatory follow-up commits
the switch, starts a 60-second probation period, and closes only AI connections
whose chains do not contain the new node. This removes stale connections from
all older chains while preserving ordinary traffic and already-correct AI
connections.

The failure episode records attempted nodes and exit fingerprints, so it does
not bounce between duplicate exits. Three independent candidates are prepared
in parallel and at most two are live-selected per failover transaction. A
commit transaction reuses up to two already prepared candidates instead of
blocking to refill the preparation target after the second failure round. A
failed just-in-time preflight never moves the live group and does not consume
the live-selection budget. Remaining untried exits cause a short
candidate-retry backoff, not a false all-unavailable declaration. Only
exhaustion of every eligible independent exit confirms the outage, sends one
notification, and enters the longer backoff.

## Persistence and recovery

State is atomically stored after each decision. The selected node is never
proactively switched back while healthy. A failed node has a five-minute
initial cooldown and must accumulate recovery successes before it becomes a
normal candidate.

Candidate evidence is tagged with a signature of the live IPv6, DNS, and hosts
semantics copied into the isolated scanner. A path change invalidates old
eligibility evidence so candidates are revalidated under the new stack.

The LaunchAgent has `RunAtLoad` and `KeepAlive`, but contains no proxy
credentials. Uninstall moves its plist to Trash. Profile rollback restores the
latest backup and keeps newly created files in a recoverable backup location.
