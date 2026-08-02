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
