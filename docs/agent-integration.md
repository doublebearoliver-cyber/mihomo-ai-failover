# Agent integration

## Interfaces

The package exposes:

- `mihomo-ai-failover`: CLI and daemon;
- `mihomo-ai-failover-mcp`: local stdio MCP server;
- `openai-network-failover`: shared Codex and Claude Code skill.

The plugin is under `plugins/mihomo-ai-failover/`. Codex and Claude use their
own manifests but the same `.mcp.json`, launcher, and skill.

## MCP tools

Read-only by default:

- `diagnose_environment`
- `get_status`
- `run_health_check`
- `list_pools`
- `get_recent_events`
- `preview_install`
- `simulate_failover`
- `get_service_status`

Mutation-capable:

- `initialize_config`
- `install_failover`
- `start_monitor`
- `stop_monitor`
- `rollback_profile`
- `uninstall_monitor`

Tool annotations declare read-only, destructive, idempotent, and open-world
behavior. Mutation tools enforce policy in the MCP server rather than relying
on model instructions.

## Mutation policy

Mutations require both:

1. local opt-in with `MIHOMO_AI_FAILOVER_MCP_MUTATIONS=1` or
   `mcp_allow_mutations: true` in local config;
2. the exact confirmation value named by the tool description.

The normal workflow is:

1. diagnose;
2. read current status and run a health check;
3. preview;
4. explain exact changes;
5. obtain user authorization;
6. enable local mutation policy;
7. call one authorized mutation;
8. diagnose again.

Do not leave MCP mutations enabled when they are not needed.

## Generic MCP client

Use a stdio definition after installing the package with the `mcp` extra:

```json
{
  "mcpServers": {
    "mihomo-ai-failover": {
      "command": "mihomo-ai-failover-mcp",
      "args": []
    }
  }
}
```

The server writes protocol messages only to stdio and opens no TCP listener.

## Cloud-hosted agents

A cloud-hosted model cannot directly reach Mihomo on a user's Mac. This
repository intentionally does not provide a public controller or unauthenticated
bridge. A future remote integration would require explicit user authorization,
strong mutual authentication, origin restriction, replay protection, audit
logging, revocation, and a separate threat model.
