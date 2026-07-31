# Mihomo AI Failover plugin

This plugin exposes the local failover CLI through a stdio MCP server and adds
the `openai-network-failover` skill to Codex and Claude Code.

The launcher first uses an existing `mihomo-ai-failover-mcp` installation. If
none is available, it uses `uv` to run the package pinned to the plugin's
release tag. No TCP listener is opened.

See the repository root README for installation, privacy, safety, and rollback
details.
