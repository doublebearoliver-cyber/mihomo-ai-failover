# Validation

## Safe local gate

These checks do not change the live Mihomo selection:

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python scripts/scan_sensitive.py
uv run python -m build
```

Then inspect the wheel and source distribution before publishing.

## Read-only real-Mac checks

```bash
mihomo-ai-failover diagnose
mihomo-ai-failover status
mihomo-ai-failover check
mihomo-ai-failover profile-preview
mihomo-ai-failover service-status
mihomo-ai-failover providers-list
mihomo-ai-failover provider-check --provider openai
```

Verify that the generated Clash config is not listed as a write target, the
controller uses a local Unix socket, the secret value is not printed, and the
dedicated AI group is present.

For a non-OpenAI Provider, run `provider-observe` only while the user is
actively exercising that product. Observation is still read-only, but its
hostname output is machine-specific and must not be pasted into issues or test
fixtures.

## Isolated policy checks

`simulate_failover` verifies without touching the live proxy:

- two aggregate hard-failure rounds are required;
- default first-failure timing remains at most 30 seconds;
- a different observed exit is preferred;
- candidate preparation overlaps the confirmation window;
- no live selection is changed.

Unit tests additionally cover:

- hard/soft response classification;
- targeted retry that suppresses a transient single-request hard failure;
- aggregate failure rounds across different critical targets and minimum-gap
  enforcement;
- candidate preparation concurrent with the second confirmation round;
- local-network guard;
- two-sample candidate eligibility, freshness, ordering, and duplicate-exit
  removal;
- cooldown and recovery;
- recovery that requires full-path scans rather than light delay probes;
- transactional verification, rollback without connection closure, and
  60-second probation;
- two usable deep candidate samples with at least one retry-free sample;
- mandatory retry-free follow-up when the first live acceptance round only
  recovers after a hard-target retry;
- just-in-time commit preflight that rejects a stale candidate before live
  selection;
- failed commit preflight that does not consume the bounded live-selection
  budget;
- bounded rotating maintenance and scan pause during a failover episode;
- stale AI connection closure across all old chains while preserving ordinary
  and already-correct connections;
- IPv6/DNS/hosts probe-stack invalidation;
- all-unavailable single notification and backoff;
- persistent profile backup, rollback, conflicts, and path escape;
- LaunchAgent generation without proxy environment leakage;
- MCP annotations and mutation denial.
- Provider schema validation and version-2-to-version-3 config migration;
- independent Provider runtime/log paths and multi-group routing rules;
- private overlay permission, guarded writes, and non-leakage when base config
  is rewritten;
- hostname-only discovery, process evidence, hidden temporal-only candidates,
  and shared-infrastructure rejection;
- serialized cross-Provider maintenance scans and isolated connection cleanup.

## Opt-in live failure exercise

Only run this after a backup and explicit operator approval:

1. Keep ordinary browsing or a non-AI download active.
2. Choose exactly one Provider and, in its dedicated group, manually select a
   known non-working test node.
3. Record the first verified hard failure time.
4. Confirm the monitor does not switch after only one failure, while candidate
   preparation begins in the isolated scanner.
5. Confirm it selects a verified distinct exit within 20-30 seconds of the
   first failure.
6. Confirm the live-route verification passes before stale connections close.
7. Confirm only stale connections matching that Provider and not using the new
   node are closed, while ordinary traffic, other Providers, and
   already-correct connections remain.
8. Confirm the new node remains selected through the 60-second probation and
   there is no proactive switch-back.
9. Repeat with a candidate that passes isolated validation but fails the live
   verification; confirm immediate rollback and zero connection closures.

Do not stop the entire airport, expose the controller, or use Codex silence as
the failure injection.

An all-pools-unavailable episode is accepted through isolated controller tests
unless the operator explicitly authorizes a real outage exercise.
