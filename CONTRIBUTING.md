# Contributing

Contributions are welcome.

1. Create a focused branch.
2. Install development dependencies with `uv sync --all-extras --dev`.
3. Add or update tests for behavior changes.
4. Run `uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`, and the sensitive-data scan.
5. Do not include real subscriptions, controller secrets, node credentials,
   node inventories, exit IPs, or local runtime logs in an issue or commit.

Real-network tests must be opt-in. Unit and CI tests use fake controller
responses and documentation-only node names.
