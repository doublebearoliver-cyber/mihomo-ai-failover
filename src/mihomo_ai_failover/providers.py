"""Provider profiles and private local overlays.

The public package ships conservative bootstrap profiles.  Non-OpenAI
providers stay disabled until a local, read-only observation has produced
evidence for the domains used on that Mac and the user has approved routing.
"""

from __future__ import annotations

import copy
import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

DEFAULT_PROVIDER_ID = "openai"
PROVIDER_OVERLAY_VERSION = 1
OPENAI_DOMAIN_SUFFIXES = (
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "oaistatsig.com",
)
PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
PROBE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SUPPORTED_PROVIDER_PROBE_KINDS = {
    "openai_api",
    "openai_auth",
    "chatgpt_web",
    "chatgpt_ws",
    "generic_web",
    "generic_transport",
}
SHARED_INFRASTRUCTURE_SUFFIXES = (
    "cloudflare.com",
    "cloudflare.net",
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "github.com",
    "githubusercontent.com",
    "apple.com",
    "microsoft.com",
    "microsoftonline.com",
    "stripe.com",
)


class ProviderError(ValueError):
    """A provider profile or overlay is invalid or unsafe."""


def builtin_provider_templates() -> dict[str, dict[str, Any]]:
    """Return fresh public bootstrap profiles.

    The roots below identify the product itself.  They intentionally do not
    guess unrelated CDN, analytics, identity, or storage domains.  Those must
    be learned from local evidence and approved as exact domains or narrowly
    scoped suffixes.
    """

    return {
        "openai": {
            "display_name": "OpenAI / ChatGPT / Codex",
            "enabled": True,
            "group_name": "🤖 AI稳定出口",
            "domain_suffixes": list(OPENAI_DOMAIN_SUFFIXES),
            "exact_domains": [],
            "bootstrap_suffixes": ["openai.com", "chatgpt.com"],
            "process_names": ["ChatGPT", "Codex"],
            "required_probe_names": ["openai_api", "openai_auth", "chatgpt_ws"],
            "browser_ambiguous_probe_names": ["chatgpt_web"],
            "candidate_preflight_url": "https://api.openai.com/v1/models",
            "candidate_preflight_expected_status": "401",
            "active_probes": [
                {
                    "name": "openai_api",
                    "kind": "openai_api",
                    "url": "https://api.openai.com/v1/models",
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                },
                {
                    "name": "openai_auth",
                    "kind": "openai_auth",
                    "url": "https://auth.openai.com/.well-known/openid-configuration",
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                },
                {
                    "name": "chatgpt_web",
                    "kind": "chatgpt_web",
                    "url": "https://chatgpt.com/",
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                },
                {
                    "name": "chatgpt_ws",
                    "kind": "chatgpt_ws",
                    "url": "https://ws.chatgpt.com/",
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                },
            ],
        },
        "workbuddy-cn": {
            "display_name": "WorkBuddy（国内版）",
            "enabled": False,
            "group_name": "🤖 WorkBuddy稳定出口",
            "domain_suffixes": ["workbuddy.cn"],
            "exact_domains": [],
            "bootstrap_suffixes": ["workbuddy.cn"],
            "process_names": ["WorkBuddy"],
            "required_probe_names": ["workbuddy_web"],
            "browser_ambiguous_probe_names": ["workbuddy_web"],
            "candidate_preflight_url": "https://www.workbuddy.cn/work/",
            "candidate_preflight_expected_status": "200",
            "active_probes": [
                {
                    "name": "workbuddy_web",
                    "kind": "generic_web",
                    "url": "https://www.workbuddy.cn/work/",
                    "expected_statuses": [200, 301, 302],
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                }
            ],
        },
        "kimi": {
            "display_name": "Kimi",
            "enabled": False,
            "group_name": "🤖 Kimi稳定出口",
            "domain_suffixes": ["kimi.com"],
            "exact_domains": [],
            "bootstrap_suffixes": ["kimi.com"],
            "process_names": ["Kimi", "Kimi Work", "Kimi Code"],
            "required_probe_names": ["kimi_web"],
            "browser_ambiguous_probe_names": ["kimi_web"],
            "candidate_preflight_url": "https://www.kimi.com/",
            "candidate_preflight_expected_status": "200",
            "active_probes": [
                {
                    "name": "kimi_web",
                    "kind": "generic_web",
                    "url": "https://www.kimi.com/",
                    "expected_statuses": [200, 301, 302],
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                }
            ],
        },
        "minimax": {
            "display_name": "MiniMax",
            "enabled": False,
            "group_name": "🤖 MiniMax稳定出口",
            "domain_suffixes": ["minimaxi.com"],
            "exact_domains": [],
            "bootstrap_suffixes": ["minimaxi.com"],
            "process_names": ["MiniMax"],
            "required_probe_names": ["minimax_web"],
            "browser_ambiguous_probe_names": ["minimax_web"],
            "candidate_preflight_url": "https://chat.minimaxi.com/",
            "candidate_preflight_expected_status": "200",
            "active_probes": [
                {
                    "name": "minimax_web",
                    "kind": "generic_web",
                    "url": "https://chat.minimaxi.com/",
                    "expected_statuses": [200, 301, 302],
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                }
            ],
        },
        "mavis": {
            "display_name": "Mavis",
            "enabled": False,
            "group_name": "🤖 Mavis稳定出口",
            "domain_suffixes": ["mavislabs.ai"],
            "exact_domains": [],
            "bootstrap_suffixes": ["mavislabs.ai"],
            "process_names": ["Mavis"],
            "required_probe_names": ["mavis_web"],
            "browser_ambiguous_probe_names": ["mavis_web"],
            "candidate_preflight_url": "https://mavislabs.ai/",
            "candidate_preflight_expected_status": "200",
            "active_probes": [
                {
                    "name": "mavis_web",
                    "kind": "generic_web",
                    "url": "https://mavislabs.ai/",
                    "expected_statuses": [200, 301, 302],
                    "timeout_seconds": 6,
                    "connect_timeout_seconds": 4,
                }
            ],
        },
    }


def normalize_hostname(value: str) -> str:
    """Return a safe ASCII hostname without paths, wildcards, or IP literals."""

    candidate = str(value).strip().lower().rstrip(".")
    if "://" in candidate:
        candidate = str(urlparse(candidate).hostname or "").lower().rstrip(".")
    if not candidate or "/" in candidate or "*" in candidate or " " in candidate:
        raise ProviderError("invalid_provider_domain")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ProviderError("provider_domain_must_not_be_ip")
    labels = candidate.split(".")
    if len(labels) < 2 or any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        raise ProviderError("invalid_provider_domain")
    return candidate


def _unique_domains(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ProviderError("provider_domains_must_be_list")
    result: list[str] = []
    for value in values:
        domain = normalize_hostname(str(value))
        if domain not in result:
            result.append(domain)
    return result


def _unique_strings(values: Any, *, label: str) -> list[str]:
    if not isinstance(values, list):
        raise ProviderError(f"{label}_must_be_list")
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or len(item) > 120 or any(char in item for char in "\r\n\0"):
            raise ProviderError(f"invalid_{label}")
        if item not in result:
            result.append(item)
    return result


def _host_matches(host: str, values: list[str] | tuple[str, ...]) -> bool:
    return any(host == value or host.endswith(f".{value}") for value in values)


def validate_provider(provider_id: str, profile: dict[str, Any]) -> None:
    if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise ProviderError("invalid_provider_id")
    if not isinstance(profile, dict):
        raise ProviderError(f"provider_mapping_required:{provider_id}")
    display_name = str(profile.get("display_name") or "").strip()
    group_name = str(profile.get("group_name") or "").strip()
    if (
        not display_name
        or len(display_name) > 120
        or any(char in display_name for char in "\r\n\0")
    ):
        raise ProviderError(f"provider_display_name_required:{provider_id}")
    if not group_name or len(group_name) > 120 or any(char in group_name for char in ",\r\n\0"):
        raise ProviderError(f"provider_group_name_required:{provider_id}")
    suffixes = _unique_domains(profile.get("domain_suffixes", []))
    exact = _unique_domains(profile.get("exact_domains", []))
    bootstrap = _unique_domains(profile.get("bootstrap_suffixes", []))
    if not suffixes and not exact and not bootstrap:
        raise ProviderError(f"provider_domain_required:{provider_id}")
    probes = profile.get("active_probes")
    if not isinstance(probes, list) or not probes:
        raise ProviderError(f"provider_probe_required:{provider_id}")
    if len(probes) > 16:
        raise ProviderError(f"too_many_provider_probes:{provider_id}")
    names: set[str] = set()
    allowed_hosts = suffixes + exact + bootstrap
    for probe in probes:
        if not isinstance(probe, dict):
            raise ProviderError(f"provider_probe_mapping_required:{provider_id}")
        name = str(probe.get("name") or "")
        kind = str(probe.get("kind") or "")
        parsed = urlparse(str(probe.get("url") or ""))
        if (
            not PROBE_NAME_PATTERN.fullmatch(name)
            or name in names
            or kind not in SUPPORTED_PROVIDER_PROBE_KINDS
        ):
            raise ProviderError(f"invalid_provider_probe:{provider_id}")
        if parsed.scheme != "https" or not parsed.hostname:
            raise ProviderError(f"provider_probe_must_use_https:{provider_id}")
        probe_host = normalize_hostname(parsed.hostname)
        if not _host_matches(probe_host, allowed_hosts):
            raise ProviderError(f"provider_probe_host_not_routed:{provider_id}")
        if _host_matches(probe_host, SHARED_INFRASTRUCTURE_SUFFIXES):
            raise ProviderError(f"provider_probe_uses_shared_infrastructure:{provider_id}")
        timeout = int(probe.get("timeout_seconds", 0))
        connect_timeout = int(probe.get("connect_timeout_seconds", 0))
        if not 1 <= timeout <= 30 or not 1 <= connect_timeout <= timeout:
            raise ProviderError(f"provider_probe_timeout_invalid:{provider_id}")
        expected_statuses = probe.get("expected_statuses")
        if expected_statuses is not None:
            try:
                normalized_statuses = [int(value) for value in expected_statuses]
            except (TypeError, ValueError):
                normalized_statuses = []
            if (
                not isinstance(expected_statuses, list)
                or not normalized_statuses
                or any(not 100 <= value <= 599 for value in normalized_statuses)
            ):
                raise ProviderError(f"provider_probe_status_invalid:{provider_id}")
        hard_statuses = probe.get("hard_statuses")
        if hard_statuses is not None:
            try:
                normalized_hard_statuses = [int(value) for value in hard_statuses]
            except (TypeError, ValueError):
                normalized_hard_statuses = []
            if (
                not isinstance(hard_statuses, list)
                or not normalized_hard_statuses
                or any(not 100 <= value <= 599 for value in normalized_hard_statuses)
            ):
                raise ProviderError(f"provider_probe_hard_status_invalid:{provider_id}")
        hard_markers = probe.get("hard_body_markers")
        if hard_markers is not None:
            normalized_markers = _unique_strings(hard_markers, label="hard_body_markers")
            if len(normalized_markers) > 16:
                raise ProviderError(f"too_many_provider_probe_markers:{provider_id}")
        if (
            expected_statuses is not None
            and hard_statuses is not None
            and set(normalized_statuses) & set(normalized_hard_statuses)
        ):
            raise ProviderError(f"provider_probe_status_conflict:{provider_id}")
        names.add(name)
    required = _unique_strings(
        profile.get("required_probe_names", []), label="required_probe_names"
    )
    ambiguous = _unique_strings(
        profile.get("browser_ambiguous_probe_names", []),
        label="browser_ambiguous_probe_names",
    )
    if not required or any(name not in names for name in required + ambiguous):
        raise ProviderError(f"provider_probe_reference_invalid:{provider_id}")
    preflight = urlparse(str(profile.get("candidate_preflight_url") or ""))
    if preflight.scheme != "https" or not preflight.hostname:
        raise ProviderError(f"provider_preflight_must_use_https:{provider_id}")
    preflight_host = normalize_hostname(preflight.hostname)
    if not _host_matches(preflight_host, allowed_hosts):
        raise ProviderError(f"provider_preflight_host_not_routed:{provider_id}")
    if _host_matches(preflight_host, SHARED_INFRASTRUCTURE_SUFFIXES):
        raise ProviderError(f"provider_preflight_uses_shared_infrastructure:{provider_id}")
    expected = str(profile.get("candidate_preflight_expected_status") or "")
    if not re.fullmatch(r"[1-5][0-9]{2}", expected):
        raise ProviderError(f"provider_preflight_status_invalid:{provider_id}")


def _merge_profile(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in {"id", "source"}:
            continue
        result[key] = copy.deepcopy(value)
    return result


def normalize_providers(raw: Any = None) -> dict[str, dict[str, Any]]:
    templates = builtin_provider_templates()
    if raw is not None:
        if not isinstance(raw, dict):
            raise ProviderError("providers_must_be_mapping")
        for provider_id, override in raw.items():
            provider_key = str(provider_id)
            if not isinstance(override, dict):
                raise ProviderError(f"provider_mapping_required:{provider_key}")
            base = templates.get(provider_key, {})
            templates[provider_key] = _merge_profile(base, override)
    for provider_id, profile in templates.items():
        profile["id"] = provider_id
        profile.setdefault("enabled", False)
        profile.setdefault("exact_domains", [])
        profile.setdefault("bootstrap_suffixes", list(profile.get("domain_suffixes", [])))
        profile.setdefault("process_names", [])
        profile["domain_suffixes"] = _unique_domains(profile["domain_suffixes"])
        profile["exact_domains"] = _unique_domains(profile["exact_domains"])
        profile["bootstrap_suffixes"] = _unique_domains(profile["bootstrap_suffixes"])
        profile["process_names"] = _unique_strings(profile["process_names"], label="process_names")
        validate_provider(provider_id, profile)
    return templates


def load_provider_overlay(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        return {"overlay_version": PROVIDER_OVERLAY_VERSION, "providers": {}}
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProviderError(f"provider_overlay_invalid:{type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ProviderError("provider_overlay_root_must_be_mapping")
    version = int(raw.get("overlay_version", 0))
    if version != PROVIDER_OVERLAY_VERSION:
        raise ProviderError("provider_overlay_version_unsupported")
    providers = raw.get("providers", {})
    if not isinstance(providers, dict):
        raise ProviderError("provider_overlay_providers_must_be_mapping")
    return {"overlay_version": version, "providers": providers}


def merge_provider_overlay(
    providers: dict[str, dict[str, Any]],
    overlay: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    combined = copy.deepcopy(providers)
    overrides = overlay.get("providers", {})
    if not isinstance(overrides, dict):
        raise ProviderError("provider_overlay_providers_must_be_mapping")
    for provider_id, value in overrides.items():
        key = str(provider_id)
        if key not in combined:
            raise ProviderError(f"overlay_unknown_provider:{key}")
        if not isinstance(value, dict):
            raise ProviderError(f"provider_mapping_required:{key}")
        combined[key] = _merge_profile(combined[key], value)
    return normalize_providers(combined)


def enabled_provider_ids(config: dict[str, Any]) -> list[str]:
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        return []
    return [
        provider_id
        for provider_id, profile in providers.items()
        if isinstance(profile, dict) and bool(profile.get("enabled"))
    ]


def resolve_provider_config(config: dict[str, Any], provider_id: str) -> dict[str, Any]:
    providers = config.get("providers", {})
    profile = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(profile, dict):
        raise ProviderError(f"unknown_provider:{provider_id}")
    result = copy.deepcopy(config)
    result["provider_id"] = provider_id
    result["provider_display_name"] = str(profile["display_name"])
    result["shared_runtime_path"] = str(config["runtime_path"])
    result["group_name"] = str(profile["group_name"])
    result["ai_domain_suffixes"] = list(profile.get("domain_suffixes", []))
    result["ai_exact_domains"] = list(profile.get("exact_domains", []))
    result["active_probes"] = copy.deepcopy(profile["active_probes"])
    result["required_probe_names"] = list(profile["required_probe_names"])
    result["browser_ambiguous_probe_names"] = list(profile.get("browser_ambiguous_probe_names", []))
    result["candidate_preflight_url"] = str(profile["candidate_preflight_url"])
    result["candidate_preflight_expected_status"] = str(
        profile["candidate_preflight_expected_status"]
    )
    if provider_id != DEFAULT_PROVIDER_ID:
        runtime = Path(str(config["runtime_path"])) / "providers" / provider_id
        log_root = Path(str(config["log_path"])).parent / "providers" / provider_id
        result["runtime_path"] = str(runtime)
        result["log_path"] = str(log_root / "monitor.jsonl")
    return result


def routing_profiles(config: dict[str, Any]) -> list[dict[str, Any]]:
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        return []
    return [
        copy.deepcopy(profile)
        for profile in providers.values()
        if isinstance(profile, dict) and bool(profile.get("enabled"))
    ]
