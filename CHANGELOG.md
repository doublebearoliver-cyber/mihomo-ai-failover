# Changelog

All notable changes to this project will be documented here.

## [Unreleased]

### Changed

- Made the shared Agent Skill the canonical machine-facing usage contract.
- Added explicit agent discovery, environment limits, tool selection, mutation
  consent, stop conditions, and privacy-safe reporting guidance.
- Added contract tests covering every MCP tool and confirmation guard.
- Added time-limited real-browser feedback keyed by observed exit fingerprint,
  with rejected-exit exclusion, confirmed-exit preference, CLI/MCP mutation
  guards, and automatic invalidation when the exit changes.
- Replaced per-target failure streaks with guarded aggregate hard-failure
  rounds and run bounded isolated candidate preparation in parallel with the
  second confirmation round.
- Added two-sample full-path candidate eligibility, live-route transactional
  verification, immediate rollback, and a 60-second post-switch probation.
- Added ChatGPT WebSocket transport probing and split exact browser challenges
  from generic soft-unstable responses.
- Added bounded active/warm/cold pool self-healing and invalidation when live
  IPv6, DNS, or hosts semantics change.
- Close stale OpenAI connections across every old chain only after a switch is
  verified; ordinary and already-correct connections remain untouched.
- Added a just-in-time live-core candidate gate and reduced rotating pool-scan
  load, with maintenance paused during confirmation, switching, and probation.
- Retry only hard-failing targets within each health round so one transient
  request timeout cannot contribute to a failover decision.
- Require two usable deep candidate samples with at least one retry-free result;
  a retry-assisted live acceptance must pass a second retry-free round after
  three seconds or be rolled back.
- Prepare three independent failover candidates while bounding live selections
  to two; a failed just-in-time preflight no longer consumes that budget.
- Reuse already prepared candidates at commit time instead of blocking after
  the second failure round merely to refill the preparation target.

## [0.1.0] - 2026-07-31

### Added

- OpenAI API, authentication, and ChatGPT web health classification.
- Two-consecutive-hard-failure switching with local-network guard.
- Active, warm, and cold pools with exit-IP deduplication and ASN diversity.
- Isolated Mihomo candidate scanner, cooldown, recovery, and outage backoff.
- Targeted stale OpenAI connection cleanup and macOS notifications.
- Persistent Clash Verge Groups/Rules integration with backup and rollback.
- Per-user LaunchAgent lifecycle and auto-discovered local configuration.
- CLI, stdio MCP server, and shared Codex/Claude Code plugin skill.
- MIT license, privacy policy, security policy, and repository instructions.
