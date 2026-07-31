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
```

Verify that the generated Clash config is not listed as a write target, the
controller uses a local Unix socket, the secret value is not printed, and the
dedicated AI group is present.

## Isolated policy checks

`simulate_failover` verifies without touching the live proxy:

- two hard failures are required;
- default first-failure timing remains at most 30 seconds;
- a different observed exit is preferred;
- no live selection is changed.

Unit tests additionally cover:

- hard/soft response classification;
- same-target failure streaks;
- local-network guard;
- candidate ordering and duplicate-exit removal;
- cooldown and recovery;
- targeted connection closure;
- all-unavailable single notification and backoff;
- persistent profile backup, rollback, conflicts, and path escape;
- LaunchAgent generation without proxy environment leakage;
- MCP annotations and mutation denial.

## Opt-in live failure exercise

Only run this after a backup and explicit operator approval:

1. Keep ordinary browsing or a non-AI download active.
2. In the dedicated AI group, manually select a known non-working test node.
3. Record the first verified hard failure time.
4. Confirm the monitor does not switch after only one failure.
5. Confirm it selects a verified distinct exit within 20-30 seconds of the
   first failure.
6. Confirm only stale AI connections on the old chain are closed.
7. Confirm ordinary traffic continues and no proactive switch-back occurs.

Do not stop the entire airport, expose the controller, or use Codex silence as
the failure injection.

An all-pools-unavailable episode is accepted through isolated controller tests
unless the operator explicitly authorizes a real outage exercise.
