# Agent integration

## Canonical agent contract

Every model must read
[`plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md`](../plugins/mihomo-ai-failover/skills/openai-network-failover/SKILL.md)
in full before using the MCP server. That `SKILL.md` is the canonical
machine-facing workflow and safety contract.

Use the files in this order:

1. the live MCP tool result for current state;
2. the live MCP tool description and annotations for exact action semantics;
3. the canonical `SKILL.md` for workflow, boundaries, and stop conditions;
4. this document for interface reference;
5. the root README for human installation and project background.

If documentation and live state differ, stop and report the mismatch. Never
invent a path, confirmation value, node status, or successful operation.

This layout follows the
[Agent Skills specification](https://agentskills.io/specification), the
[OpenAI plugins repository structure](https://github.com/openai/plugins), and
the MCP requirement for
[explicit consent before local server commands](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/1024-mcp-client-security-requirements-for-local-server-.md).
The project keeps one operational contract in `SKILL.md` instead of creating a
second, competing model-instruction format.

## Interfaces

The package exposes:

- `mihomo-ai-failover`: CLI and daemon;
- `mihomo-ai-failover-mcp`: local stdio MCP server;
- `openai-network-failover`: shared Codex and Claude Code skill.

The plugin is under `plugins/mihomo-ai-failover/`. Codex and Claude use their
own manifests but the same `.mcp.json`, launcher, and skill.

## Supported agent environments

- Codex or Claude Code with the repository plugin installed;
- another local agent that can start a trusted stdio MCP server and load the
  canonical `SKILL.md`;
- macOS with Clash Verge Rev and Mihomo.

The skill is not a remote-control protocol. A cloud-only model, a chat webpage,
or an agent without local MCP access can explain results but cannot inspect or
operate the user's Mac.

## MCP tools

| Tool | Class | Intended use |
| --- | --- | --- |
| `diagnose_environment` | Read-only, local | First call for every task |
| `get_status` | Read-only, local | Current group, health, and pool summary |
| `run_health_check` | Read-only, network | Real OpenAI path check; never switches |
| `list_pools` | Read-only, local | Pool counts and independent-exit coverage |
| `get_recent_events` | Read-only, local | Sanitized evidence for recent behavior |
| `preview_install` | Read-only, local | Exact persistent changes before install |
| `simulate_failover` | Read-only, local | Isolated invariant test; no live change |
| `get_service_status` | Read-only, local | LaunchAgent installation and run state |
| `initialize_config` | Mutation, local | Write the discovered local config |
| `install_failover` | Mutation, local | Back up and install profile integration |
| `start_monitor` | Mutation, local | Start or restart the LaunchAgent |
| `stop_monitor` | Mutation, local | Stop the LaunchAgent |
| `record_web_feedback` | Mutation, local | Store time-limited, exit-bound real-browser evidence while the monitor is stopped |
| `rollback_profile` | Destructive, local | Restore a profile integration backup |
| `uninstall_monitor` | Destructive, local | Stop and remove the LaunchAgent plist |

Tool annotations declare read-only, destructive, idempotent, and open-world
behavior. Mutation tools enforce policy in the MCP server rather than relying
on model instructions.

## Evidence rules

| Signal | Agent interpretation |
| --- | --- |
| Same OpenAI target has two consecutive verified hard failures | The daemon may evaluate failover after local-network and controller guards |
| TCP/TLS failure, timeout, reset, or verified unavailable/region response | Hard-failure evidence when classified by the health checker |
| One failed probe, small latency change, or a slow response | Soft anomaly; observe, do not switch |
| Codex spins or temporarily has no output | Auxiliary symptom only |
| Cloudflare browser challenge | Browser state, not healthy-path proof and not independent node-failure proof |
| User explicitly verifies ChatGPT login success/failure in a real browser | May be recorded as time-limited exit-fingerprint feedback; never infer it from an automated challenge page |
| GitHub, Git, npm, Docker, or an ordinary website fails | Outside the OpenAI failover trigger |

`run_health_check` is a snapshot and never switches nodes. Use sanitized recent
events to understand a sequence; do not manufacture a two-failure sequence from
one result.

## Mutation policy

Mutations require both:

1. local opt-in with `MIHOMO_AI_FAILOVER_MCP_MUTATIONS=1` or
   `mcp_allow_mutations: true` in local config;
2. the exact confirmation value named by the tool description.

| Tool | Exact confirmation |
| --- | --- |
| `initialize_config` | `WRITE_LOCAL_CONFIG` |
| `install_failover` | `INSTALL_MIHOMO_AI_FAILOVER` |
| `start_monitor` | `START_LAUNCH_AGENT` |
| `stop_monitor` | `STOP_LAUNCH_AGENT` |
| `record_web_feedback` | `RECORD_WEB_FEEDBACK` |
| `rollback_profile` | `ROLLBACK_PROFILE_INTEGRATION` |
| `uninstall_monitor` | `UNINSTALL_LAUNCH_AGENT` |

These values are action guards, not authentication secrets and not standing
consent. The current tool description is authoritative. A model must never
guess, normalize, reuse for a different action, or treat a confirmation value
as a substitute for the user's explicit authorization.

The normal workflow is:

1. diagnose;
2. read current status and run a health check;
3. preview;
4. explain exact changes;
5. obtain user authorization;
6. enable local mutation policy;
7. call one authorized mutation;
8. diagnose again.

For browser feedback, first establish the node and observed exit through
read-only status/inventory evidence, obtain an explicit real-browser result
from the user, stop the monitor, call `record_web_feedback`, and restart the
monitor. The tool rejects missing exit fingerprints and never changes the live
proxy selection. A rejected result expires by default after 24 hours; a
confirmed result expires after seven days; a changed exit fingerprint
invalidates either result immediately.

Do not leave MCP mutations enabled when they are not needed.

Stop before mutation when the preview fails, the environment is unsupported,
the target path or controller is ambiguous, the request would alter TUN or a
global proxy group, or the user has not authorized the exact action.

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

For an agent without plugin discovery:

1. install the package from a pinned release;
2. configure the stdio server above in a trusted local client;
3. provide the canonical `SKILL.md` to the model as its operational
   instructions;
4. begin with `diagnose_environment`.

Do not paste subscription URLs, controller secrets, proxy credentials, exit
IPs, or full inventories into model prompts.

## Cloud-hosted agents

A cloud-hosted model cannot directly reach Mihomo on a user's Mac. This
repository intentionally does not provide a public controller or unauthenticated
bridge. A future remote integration would require explicit user authorization,
strong mutual authentication, origin restriction, replay protection, audit
logging, revocation, and a separate threat model.

## Result contract

An agent response must distinguish observed evidence from inference and state:

- the verified outcome and failing layer;
- whether the work was read-only or mutating;
- whether a node switch occurred;
- which files or services changed, if any;
- whether Clash Verge must be restarted;
- the smallest safe next action.

Never expose controller secrets, subscriptions, proxy credentials, server
addresses, exit IPs, or a full node inventory.
