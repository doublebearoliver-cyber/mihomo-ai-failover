"""Read-only local Provider discovery and guarded private-overlay writes."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import yaml

from .providers import (
    PROVIDER_OVERLAY_VERSION,
    SHARED_INFRASTRUCTURE_SUFFIXES,
    ProviderError,
    load_provider_overlay,
    normalize_hostname,
    validate_provider,
)

PROVIDER_OVERLAY_CONFIRMATION = "APPLY_PROVIDER_OVERLAY"


class ControllerLike(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> Any: ...


def _matches_suffix(host: str, suffixes: list[str] | tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in suffixes)


def _connection_snapshot(controller: ControllerLike) -> dict[str, dict[str, Any]]:
    payload = controller.request("GET", "/connections", timeout=5.0)
    connections = payload.get("connections", []) if isinstance(payload, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for item in connections:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        raw_host = str(metadata.get("host") or "").strip()
        if not raw_host:
            continue
        try:
            host = normalize_hostname(raw_host)
        except ProviderError:
            continue
        process_value = str(
            metadata.get("process")
            or metadata.get("processPath")
            or metadata.get("process-path")
            or ""
        )
        process_name = Path(process_value).name[:120] if process_value else ""
        record = result.setdefault(
            host,
            {
                "host": host,
                "process_names": set(),
                "samples": 0,
            },
        )
        if process_name:
            record["process_names"].add(process_name)
        record["samples"] = int(record["samples"]) + 1
    return result


def _process_matches(observed: set[str], expected: list[str]) -> bool:
    lowered = {item.lower() for item in observed}
    return any(
        candidate.lower() == item or candidate.lower() in item
        for candidate in expected
        for item in lowered
    )


def observe_provider_connections(
    controller: ControllerLike,
    config: dict[str, Any],
    provider_id: str,
    *,
    duration_seconds: int = 10,
    poll_interval_seconds: float = 1.0,
    include_temporal_candidates: bool = False,
) -> dict[str, Any]:
    """Observe hostnames only; never return paths, connection IDs, IPs, or chains."""

    providers = config.get("providers", {})
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ProviderError(f"unknown_provider:{provider_id}")
    duration = max(1, min(int(duration_seconds), 60))
    interval = max(0.25, min(float(poll_interval_seconds), 5.0))
    baseline = _connection_snapshot(controller)
    observed: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + duration
    while True:
        snapshot = _connection_snapshot(controller)
        for host, item in snapshot.items():
            record = observed.setdefault(
                host,
                {
                    "host": host,
                    "process_names": set(),
                    "samples": 0,
                },
            )
            record["process_names"].update(item["process_names"])
            record["samples"] = int(record["samples"]) + int(item["samples"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    known_suffixes = list(provider.get("domain_suffixes", [])) + list(
        provider.get("bootstrap_suffixes", [])
    )
    known_exact = {str(item) for item in provider.get("exact_domains", [])}
    expected_processes = [str(item) for item in provider.get("process_names", [])]
    candidates: list[dict[str, Any]] = []
    for host in sorted(observed):
        item = observed[host]
        known = host in known_exact or _matches_suffix(host, known_suffixes)
        process_match = _process_matches(item["process_names"], expected_processes)
        temporal_new = host not in baseline
        if not (known or process_match or (temporal_new and include_temporal_candidates)):
            continue
        shared = _matches_suffix(host, SHARED_INFRASTRUCTURE_SUFFIXES)
        if known:
            confidence, evidence = "high", "known_provider_domain"
        elif process_match:
            confidence, evidence = "medium", "provider_process"
        else:
            confidence, evidence = "low", "temporal_only"
        already_routed = host in known_exact or _matches_suffix(
            host, list(provider.get("domain_suffixes", []))
        )
        candidates.append(
            {
                "host": host,
                "confidence": confidence,
                "evidence": evidence,
                "samples": int(item["samples"]),
                "already_routed": already_routed,
                "shared_infrastructure": shared,
                "recommended_exact_domain": bool(
                    not already_routed and confidence in {"high", "medium"} and not shared
                ),
            }
        )
    return {
        "provider_id": provider_id,
        "provider": provider.get("display_name"),
        "duration_seconds": duration,
        "connection_samples": sum(int(item["samples"]) for item in observed.values()),
        "candidates": candidates,
        "recommended_exact_domains": [
            item["host"] for item in candidates if item["recommended_exact_domain"]
        ],
        "limitations": [
            (
                "temporal_only_candidates_require_user_review"
                if include_temporal_candidates
                else "temporal_only_candidates_hidden"
            ),
            "shared_infrastructure_is_never_auto_recommended",
            "browser_processes_cannot_prove_tab_ownership",
        ],
    }


def preview_provider_overlay(
    config: dict[str, Any],
    provider_id: str,
    *,
    exact_domains: list[str],
    critical_domains: list[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    providers = config.get("providers", {})
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ProviderError(f"unknown_provider:{provider_id}")
    normalized: list[str] = []
    for value in exact_domains:
        host = normalize_hostname(value)
        if host not in normalized:
            normalized.append(host)
    critical: list[str] = []
    for value in critical_domains or []:
        host = normalize_hostname(value)
        if _matches_suffix(host, SHARED_INFRASTRUCTURE_SUFFIXES):
            raise ProviderError(f"critical_provider_domain_is_shared_infrastructure:{host}")
        if host not in critical:
            critical.append(host)
        if host not in normalized:
            normalized.append(host)
    if len(critical) > 8:
        raise ProviderError("too_many_critical_provider_domains")
    current = [str(item) for item in provider.get("exact_domains", [])]
    combined = list(current)
    for host in normalized:
        if host not in combined and not _matches_suffix(
            host, list(provider.get("domain_suffixes", []))
        ):
            combined.append(host)
    if len(combined) > 64:
        raise ProviderError("too_many_exact_provider_domains")
    changes = [f"add_exact_domain:{host}" for host in combined if host not in current]
    if enabled is not None and bool(provider.get("enabled")) != bool(enabled):
        changes.append(f"set_enabled:{str(bool(enabled)).lower()}")
    probes = [dict(item) for item in provider.get("active_probes", [])]
    required = [str(item) for item in provider.get("required_probe_names", [])]
    probe_names_by_host: dict[str, list[str]] = {}
    for probe in probes:
        try:
            hostname = str(urlparse(str(probe.get("url") or "")).hostname or "")
        except ValueError:
            hostname = ""
        if hostname:
            probe_host = hostname.lower().rstrip(".")
            probe_names_by_host.setdefault(probe_host, []).append(str(probe.get("name") or ""))
    for host in critical:
        if host in probe_names_by_host:
            for existing_name in probe_names_by_host[host]:
                if existing_name and existing_name not in required:
                    required.append(existing_name)
                    changes.append(f"require_existing_probe:{host}")
            continue
        name = f"observed_transport_{hashlib.sha256(host.encode()).hexdigest()[:10]}"
        probes.append(
            {
                "name": name,
                "kind": "generic_transport",
                "url": f"https://{host}/",
                "expected_statuses": [200, 301, 302, 400, 401, 403, 404, 426],
                "timeout_seconds": 6,
                "connect_timeout_seconds": 4,
            }
        )
        required.append(name)
        changes.append(f"add_critical_transport_probe:{host}")
    override: dict[str, Any] = {
        "exact_domains": combined,
        "active_probes": probes,
        "required_probe_names": required,
    }
    if enabled is not None:
        override["enabled"] = bool(enabled)
    prospective = dict(provider)
    prospective.update(override)
    validate_provider(provider_id, prospective)
    exact_domain_changes = [item for item in changes if item.startswith("add_exact_domain:")]
    enabling_now = enabled is True and not bool(provider.get("enabled"))
    profile_change = bool(enabling_now or (prospective.get("enabled") and exact_domain_changes))
    return {
        "provider_id": provider_id,
        "overlay_path": str(config["provider_overlay_path"]),
        "changes": changes,
        "already_configured": not changes,
        "provider_override": override,
        "requires_profile_reinstall": profile_change,
        "requires_clash_restart": profile_change,
        "requires_monitor_restart": bool(changes),
        "profile_cleanup_on_disable": "manual_or_rollback",
    }


def apply_provider_overlay(
    config: dict[str, Any],
    provider_id: str,
    *,
    exact_domains: list[str],
    critical_domains: list[str] | None = None,
    enabled: bool | None = None,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != PROVIDER_OVERLAY_CONFIRMATION:
        raise ProviderError("explicit_confirmation_required")
    preview = preview_provider_overlay(
        config,
        provider_id,
        exact_domains=exact_domains,
        critical_domains=critical_domains,
        enabled=enabled,
    )
    target = Path(str(config["provider_overlay_path"])).expanduser()
    overlay = load_provider_overlay(target)
    providers = overlay.setdefault("providers", {})
    existing = providers.setdefault(provider_id, {})
    if not isinstance(existing, dict):
        raise ProviderError(f"provider_mapping_required:{provider_id}")
    existing.update(preview["provider_override"])
    document = {
        "overlay_version": PROVIDER_OVERLAY_VERSION,
        "providers": providers,
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return {
        **preview,
        "changed": not preview["already_configured"],
        "written": True,
    }
