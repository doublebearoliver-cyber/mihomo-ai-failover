# Privacy

Mihomo AI Failover runs locally. It has no analytics, telemetry, crash upload,
account system, or author-operated service. It does not upload subscriptions,
node credentials, controller secrets, runtime logs, or node inventories to the
project author.

## Default network destinations

Operational probes are intentionally explicit:

| Destination | Path | Purpose | Route |
| --- | --- | --- | --- |
| `api.openai.com` | `/v1/models` | Validate a real OpenAI JSON authentication response | selected AI proxy |
| `auth.openai.com` | OIDC discovery | Validate authentication reachability and issuer | selected AI proxy |
| `chatgpt.com` | `/` | Validate web reachability; browser challenge is soft | selected AI proxy |
| `ws.chatgpt.com` | `/` | Validate ChatGPT TCP/TLS/HTTP transport | selected OpenAI proxy |
| `www.workbuddy.cn` | `/work/` | Disabled bootstrap probe for WorkBuddy (China) | selected WorkBuddy proxy when enabled |
| `www.kimi.com` | `/` | Disabled bootstrap probe for Kimi | selected Kimi proxy when enabled |
| `chat.minimaxi.com` | `/` | Disabled bootstrap probe for MiniMax | selected MiniMax proxy when enabled |
| `mavislabs.ai` | `/` | Disabled bootstrap probe for Mavis | selected Mavis proxy when enabled |
| `captive.apple.com` | captive portal page | Distinguish local-network failure | direct |
| `1.1.1.1` | Cloudflare trace | Second direct-network signal | direct |
| `api.ip.sb` | GeoIP JSON | Observe candidate exit IP, region, and ASN | isolated candidate proxy |

Non-OpenAI bootstrap probes are not contacted until their Provider is enabled
or a user/agent explicitly runs its read-only path check. Raw response bodies
are used in memory only for validation and are not written to logs. Operators
can replace or extend probe URLs in the local private overlay.

The plugin launcher may access GitHub and the configured Python package index
on first bootstrap through `uv`. After the command is installed, normal MCP
startup reuses the local installation/cache.

## Local data

Default locations:

- config and state:
  `~/Library/Application Support/Mihomo AI Failover/`
- private Provider overlay:
  `~/Library/Application Support/Mihomo AI Failover/providers.local.yaml`
- backups:
  `~/Library/Application Support/Mihomo AI Failover/backups/`
- logs:
  `~/Library/Logs/Mihomo AI Failover/`

Local state may contain:

- proxy display names and protocols;
- observed public exit IP, country, region, ASN, and organization;
- health statistics, latency samples, cooldown timestamps, and switch history;
- time-limited user-confirmed browser status, sanitized reason, and the exit
  fingerprint to which that feedback applies;
- sanitized error categories.
- operator-approved exact Provider hostnames and probe endpoints in the private
  overlay.
- a timestamp and Provider ID used only to deduplicate an airport-wide Toast
  across local Provider state machines.

Local state must not contain proxy server addresses, proxy passwords,
subscription URLs, raw Provider responses, or the Mihomo controller secret.
Runtime directories are user-only and runtime files are excluded from Git.

The read-only discovery workflow samples Mihomo's local connection API. It
keeps only normalized hostnames, process basenames, evidence categories, and
sample counts in memory. It never returns URL paths, connection IDs, remote
IPs, chains, credentials, or full node definitions. Nothing is persisted until
the operator authorizes a private overlay write.

## MCP output

The stdio MCP server does not open a network listener. It never returns exit
IPs, controller secrets, subscriptions, proxy credentials, or full proxy
definitions. Node names are hidden unless a caller explicitly opts in.
The dedicated discovery tool may return sanitized observed hostnames because
that is its explicit purpose; temporal-only hosts are hidden by default and
never auto-recommended.

## Deletion

Stop the service, roll back the profile integration, uninstall the
LaunchAgent, and remove the application-data and log directories if local
history is no longer needed. Review backups before deleting them.
