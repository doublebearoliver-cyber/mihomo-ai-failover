# Mihomo AI Failover plugin

This plugin exposes the local failover CLI through a stdio MCP server and adds
the `openai-network-failover` skill to Codex and Claude Code.

The legacy skill name remains for version-0.x compatibility. Its current
contract covers isolated profiles for OpenAI, WorkBuddy (China), Kimi, MiniMax,
and Mavis, plus private local adaptation from observed Mihomo traffic.

## Agent entrypoint

Before calling any MCP tool, read
[`skills/openai-network-failover/SKILL.md`](skills/openai-network-failover/SKILL.md)
in full. It is the canonical agent contract for:

- when this plugin should and should not be used;
- read-only-first diagnosis and evidence classification;
- mutation authorization and stop conditions;
- privacy-safe result reporting.
- Provider discovery evidence, independent group boundaries, and private
  overlay workflow.

Start with `diagnose_environment`. Do not infer a node failure from a spinning
Codex UI, one slow request, or a small latency change. Mutations are disabled by
default and require both local opt-in and exact, server-enforced confirmation.
Before enabling a non-OpenAI Provider, read the Skill's
`references/provider-adaptation.md`, call the read-only Provider discovery
tools, and preview the exact local overlay. Never guess domains or reuse one
Provider's failure state for another.

## Runtime

The launcher first uses an existing `mihomo-ai-failover-mcp` installation. If
none is available, it uses `uv` to run the package pinned to the plugin's
release tag. No TCP listener is opened.

See the [repository root README](../../README.md) for human installation,
privacy, safety, and rollback details. A cloud-only model cannot use this
plugin to reach a user's Mac without a trusted local MCP client.
