# Repository instructions

## Scope

- This repository provides OpenAI/ChatGPT/Codex failover for Mihomo-compatible
  clients. Version 0.x supports macOS and Clash Verge Rev.
- Preserve the current AI-only routing boundary. Never switch a global proxy
  group, enable TUN, disable the system proxy, or alter unrelated traffic.
- Keep the failover engine usable without an AI agent. The CLI and daemon are
  authoritative; MCP and skills are adapters.

## Safety and privacy

- Never commit subscription URLs, controller secrets, proxy passwords, node
  server addresses, private node inventories, exit IPs, or runtime logs.
- Read the Mihomo controller secret only at runtime from a local config file.
- Prefer a Unix-domain controller socket. Never make an external controller
  listen on a non-loopback address.
- Never hard-code a username, home directory, application data path, or local
  repository path.
- MCP tools are read-only by default. State-changing tools must accurately set
  MCP safety annotations and require an explicit confirmation token enforced
  by the server, not merely by model instructions.
- Tests must use fake nodes, fake IPs, temporary directories, and fake
  controller responses.

## Failover invariants

- A healthy selected node stays selected. Latency differences alone never
  trigger a switch.
- Switching requires two guarded aggregate hard-failure rounds on the verified
  OpenAI route; the failing critical target may differ between rounds.
- A local-network failure, stopped Mihomo process, or controller failure must
  not trigger blind switching.
- Candidate ranking prioritizes current health, success history, a distinct
  exit IP, ASN diversity, cooldown state, and stability before latency.
- Deep candidate evidence needs two usable samples with at least one retry-free
  result. A retry-assisted live acceptance must pass a mandatory retry-free
  follow-up before a newly selected route can commit.
- After switching, close only stale OpenAI-related connections whose chains do
  not contain the newly selected node.
- Notify "当前机场全部不可用" once per outage episode and use backoff.

## Code layout

- `src/mihomo_ai_failover/engine.py`: failover behavior and state model.
- `src/mihomo_ai_failover/`: reusable configuration, platform, service, CLI,
  and MCP adapters.
- `skills/`: one shared workflow used by Codex and Claude Code.
- `.codex-plugin/` and `.claude-plugin/`: ecosystem identity manifests.
- `tests/`: unit and isolated integration tests.

## Development workflow

1. Run `uv sync --all-extras --dev`.
2. Run `uv run pytest`.
3. Run `uv run ruff check .`.
4. Run `uv run ruff format --check .`.
5. Run `uv run python scripts/scan_sensitive.py`.
6. For MCP changes, run the MCP contract tests and inspect the tool list.
7. Real Mihomo tests are opt-in and must start read-only. Never force a live
   node failure unless the user explicitly requested that test.

## Compatibility

- Python source targets Python 3.10 or newer.
- Do not rely on the macOS system Python. Installation uses an isolated
  environment or a release artifact.
- Keep public CLI and MCP tool names backward compatible within a major
  version.

## Documentation and releases

- Update both `README.md` and `README.zh-CN.md` when user-visible behavior
  changes.
- Document every new external data destination in `PRIVACY.md`.
- Before a release, build the wheel and source distribution, inspect their
  contents, run the full test suite, and scan the Git history for secrets.
