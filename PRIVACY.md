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
| `captive.apple.com` | captive portal page | Distinguish local-network failure | direct |
| `1.1.1.1` | Cloudflare trace | Second direct-network signal | direct |
| `api.ip.sb` | GeoIP JSON | Observe candidate exit IP, region, and ASN | isolated candidate proxy |

Raw response bodies are used in memory only for validation and are not written
to logs. Operators can replace probe URLs in the local config.

The plugin launcher may access GitHub and the configured Python package index
on first bootstrap through `uv`. After the command is installed, normal MCP
startup reuses the local installation/cache.

## Local data

Default locations:

- config and state:
  `~/Library/Application Support/Mihomo AI Failover/`
- backups:
  `~/Library/Application Support/Mihomo AI Failover/backups/`
- logs:
  `~/Library/Logs/Mihomo AI Failover/`

Local state may contain:

- proxy display names and protocols;
- observed public exit IP, country, region, ASN, and organization;
- health statistics, latency samples, cooldown timestamps, and switch history;
- sanitized error categories.

Local state must not contain proxy server addresses, proxy passwords,
subscription URLs, raw OpenAI responses, or the Mihomo controller secret.
Runtime directories are user-only and runtime files are excluded from Git.

## MCP output

The stdio MCP server does not open a network listener. It never returns exit
IPs, controller secrets, subscriptions, proxy credentials, or full proxy
definitions. Node names are hidden unless a caller explicitly opts in.

## Deletion

Stop the service, roll back the profile integration, uninstall the
LaunchAgent, and remove the application-data and log directories if local
history is no longer needed. Review backups before deleting them.
