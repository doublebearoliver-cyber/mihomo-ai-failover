# Provider adaptation workflow

Read this file only when discovering, enabling, or changing a Provider.

## Public and personal layers

- Public layer: reviewed code, conservative product roots, generic safety
  rules, CLI/MCP contracts, and no machine-specific observations.
- Personal layer: `providers.local.yaml`, mode `0600`, containing only the
  operator-approved enablement, exact domains, and probes for this Mac.
- The personal layer is an overlay, not a fork. It must remain outside the
  repository and must not be uploaded.

## Evidence levels

| Evidence | Meaning | Automatic action |
| --- | --- | --- |
| `known_provider_domain` | Matches a configured public/private root | Confirm routing coverage only |
| `provider_process` | Active connection is correlated with the Provider app process | May be proposed as an exact domain unless shared infrastructure |
| `temporal_only` | Appeared during the observation window, often from a browser | Never auto-add; require corroboration and user review |
| `shared_infrastructure` | Cloud/CDN/identity domain used by many products | Never make it a critical failover trigger |

Observation uses process basenames locally for correlation, but returns only
hostnames, evidence classes, and counts. It deliberately omits process values,
URLs, paths, connection IDs, remote IPs, chains, credentials, and node
inventories. Browser process correlation cannot prove which tab owns a host.

## Safe sequence

1. `diagnose_environment`
2. `list_provider_profiles`
3. `check_provider_paths(provider_id=...)`
4. Ask the user to open/login/start a conversation or generation in only the
   target Provider.
5. `discover_provider_domains(provider_id=..., duration_seconds=10..60)`
6. If browser-only evidence is necessary, rerun with temporal candidates
   visible, but keep them unapproved until corroborated by a second workflow or
   authoritative Provider documentation.
7. `preview_provider_overlay` with the smallest exact-domain set. Critical
   domains must be a subset whose TCP/TLS/HTTP failure objectively makes the
   Provider unusable.
8. Explain the preview and obtain authorization.
9. Enable mutation policy for this one action and call
   `apply_provider_overlay` with `APPLY_PROVIDER_OVERLAY`.
10. `preview_install`, then obtain separate authorization for profile/service
    mutation.
11. Apply installation, restart Clash Verge when required, and run
    Provider-specific health and inventory checks.
12. Start/restart the LaunchAgent and return to read-only tools.

If the direct path is healthy and consistently better while the Provider does
not require a proxy, do not force it into a failover group. Leave its existing
direct/rule behavior unchanged. If the app is active but its key connections do
not appear in Mihomo, report possible system-proxy bypass; do not enable TUN or
claim the Provider is covered without a same-node, separately authorized A/B
test.

## Probe limits

- `generic_transport` proves TCP/TLS and an allowed HTTP response only. It does
  not prove authentication, account state, streaming correctness, or model
  output.
- A status/body rule may be treated as hard only after its unavailable/region
  semantics were verified. A generic `200`, login page, challenge page, or
  error shell is not proof of service health.
- One slow response, one timeout, UI silence, or lack of model output is not an
  independent failover signal.
- Do not add Cloudflare, Google, Apple, Microsoft, Stripe, GitHub, or another
  shared platform as a critical target just because it appeared nearby in
  time.

## Multi-Provider resource boundary

Each enabled Provider runs an independent ten-second foreground check and
independent state machine. Enable only Providers the operator actively needs.
Background deep scans are serialized across Providers and daemon starts are
staggered, but additional enabled Providers still add bounded network and CPU
work. Disabling a Provider stops its monitor after service restart; persistent
rules remain until an authorized rollback or explicit profile cleanup.
