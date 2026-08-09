"""Configuration discovery and normalization.

The public project never embeds a user's home directory or controller secret.
Clash Verge paths are discovered at runtime and remain overridable.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from .providers import (
    DEFAULT_PROVIDER_ID,
    OPENAI_DOMAIN_SUFFIXES,
    ProviderError,
    builtin_provider_templates,
    load_provider_overlay,
    merge_provider_overlay,
    normalize_hostname,
    normalize_providers,
)

APP_DIR_NAME = "Mihomo AI Failover"
CLASH_VERGE_APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
DEFAULT_GROUP_NAME = "🤖 AI稳定出口"
DEFAULT_SOCKET_PATH = "/tmp/verge/verge-mihomo.sock"
DEFAULT_CONFIG_FILENAME = "config.yaml"
CONFIG_VERSION = 3

DEFAULT_AI_SUFFIXES = list(OPENAI_DOMAIN_SUFFIXES)

PATH_KEYS = {
    "clash_data_root",
    "clash_config_path",
    "clash_generated_config_path",
    "clash_socket_path",
    "runtime_path",
    "log_path",
    "mihomo_core_path",
    "provider_overlay_path",
}


class ConfigError(ValueError):
    """A local configuration is invalid or unsafe."""


def app_support_dir(home: Path | None = None) -> Path:
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "Application Support" / APP_DIR_NAME


def app_log_dir(home: Path | None = None) -> Path:
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "Logs" / APP_DIR_NAME


def default_config_path(home: Path | None = None) -> Path:
    return app_support_dir(home) / DEFAULT_CONFIG_FILENAME


def default_clash_root(home: Path | None = None) -> Path:
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "Application Support" / CLASH_VERGE_APP_ID


def discover_clash_config(
    home: Path | None = None,
    clash_root: Path | None = None,
) -> Path:
    root = default_clash_root(home) if clash_root is None else Path(clash_root)
    candidates = [
        root / "clash-verge.yaml",
        root / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _read_yaml_scalar(path: Path, key: str) -> Any:
    """Read one top-level YAML scalar without loading proxy credentials."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*$", re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    value = match.group(1)
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return None


def discover_mihomo_core() -> Path:
    candidates = [
        Path("/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"),
        Path("/Applications/Clash Verge Rev.app/Contents/MacOS/verge-mihomo"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    from_path = shutil.which("mihomo")
    return Path(from_path) if from_path else candidates[0]


def default_config(
    home: Path | None = None,
    clash_root: Path | None = None,
) -> dict[str, Any]:
    root = default_clash_root(home) if clash_root is None else Path(clash_root)
    generated = discover_clash_config(home, root)
    mixed_port = _read_yaml_scalar(generated, "mixed-port")
    if not isinstance(mixed_port, int) or not 1 <= mixed_port <= 65535:
        mixed_port = 7897
    socket_path = _read_yaml_scalar(generated, "external-controller-unix")
    if not isinstance(socket_path, str) or not socket_path:
        socket_path = DEFAULT_SOCKET_PATH
    runtime = app_support_dir(home)
    log_dir = app_log_dir(home)
    providers = normalize_providers(builtin_provider_templates())
    openai = providers[DEFAULT_PROVIDER_ID]

    return {
        "config_version": CONFIG_VERSION,
        "clash_data_root": str(root),
        "clash_config_path": str(generated),
        "clash_generated_config_path": str(generated),
        "clash_socket_path": socket_path,
        "runtime_path": str(runtime),
        "log_path": str(log_dir / "monitor.jsonl"),
        "provider_overlay_path": str(runtime / "providers.local.yaml"),
        "provider_overlay_loaded": False,
        "mixed_proxy_url": f"http://127.0.0.1:{mixed_port}",
        "default_provider_id": DEFAULT_PROVIDER_ID,
        "providers": providers,
        "group_name": openai["group_name"],
        "mihomo_core_path": str(discover_mihomo_core()),
        "ai_domain_suffixes": list(openai["domain_suffixes"]),
        "ai_exact_domains": list(openai["exact_domains"]),
        "required_probe_names": list(openai["required_probe_names"]),
        "browser_ambiguous_probe_names": list(openai["browser_ambiguous_probe_names"]),
        "node_exclude_regex": (
            r"(套餐|流量|到期|过期|剩余|官网|网址|更新|订阅|倍率|公告|"
            r"DIRECT|REJECT|PASS)"
        ),
        "monitor_interval_seconds": 10,
        "failure_rounds_before_switch": 2,
        "single_target_failure_rounds_before_switch": 3,
        "single_target_observation_seconds": 30,
        "fast_failover_min_distinct_targets": 2,
        "failure_confirmation_min_gap_seconds": 8,
        "hard_probe_retry_count": 1,
        "hard_probe_retry_delay_seconds": 1,
        "parallel_failure_confirmation": True,
        "switch_connection_wait_seconds": 3,
        "connection_drain_mode": "preserve",
        "max_candidate_attempts_per_failover": 2,
        "candidate_retry_backoff_seconds": 30,
        "cooldown_first_seconds": 300,
        "cooldown_repeat_seconds": 900,
        "repeat_failure_window_seconds": 21600,
        "recovery_successes_required": 3,
        "all_unavailable_backoff_seconds": 300,
        "active_pool_min": 12,
        "active_pool_max": 16,
        "warm_pool_min": 30,
        "warm_pool_max": 40,
        "deep_verification_ttl_seconds": 604800,
        "active_candidate_ttl_seconds": 1800,
        "warm_candidate_ttl_seconds": 21600,
        "web_feedback_confirmed_ttl_seconds": 604800,
        "web_feedback_rejected_ttl_seconds": 86400,
        "candidate_preflight_url": openai["candidate_preflight_url"],
        "candidate_preflight_expected_status": openai["candidate_preflight_expected_status"],
        "candidate_preflight_timeout_ms": 4000,
        "candidate_commit_preflight_timeout_ms": 2000,
        "candidate_reverification_delay_seconds": 3,
        "candidate_concurrency": 3,
        "candidate_prefilter_limit": 8,
        "candidate_prefilter_batch_size": 4,
        "candidate_prepare_count": 2,
        "candidate_isolated_parallelism": 2,
        "candidate_validation_samples_required": 2,
        "candidate_validation_window_seconds": 3600,
        "candidate_validation_min_gap_seconds": 5,
        "candidate_validation_fresh_seconds": 60,
        "probation_seconds": 60,
        "active_preflight_interval_seconds": 10,
        "active_preflight_batch_size": 2,
        "active_full_scan_interval_seconds": 300,
        "active_full_scan_batch_size": 2,
        "warm_scan_interval_seconds": 600,
        "warm_scan_batch_size": 1,
        "pool_refill_interval_seconds": 600,
        "pool_refill_batch_size": 1,
        "cold_scan_interval_seconds": 21600,
        "cold_scan_batch_size": 2,
        "catalog_refresh_interval_seconds": 300,
        "initial_deep_scan_max": 40,
        "geo_probe_url": "https://api.ip.sb/geoip",
        "geo_probe_timeout_seconds": 6,
        "mcp_allow_mutations": False,
        "direct_probes": [
            {
                "name": "apple_captive",
                "kind": "local",
                "url": "https://captive.apple.com/hotspot-detect.html",
                "timeout_seconds": 4,
                "connect_timeout_seconds": 3,
            },
            {
                "name": "cloudflare_trace",
                "kind": "local",
                "url": "https://1.1.1.1/cdn-cgi/trace",
                "timeout_seconds": 4,
                "connect_timeout_seconds": 3,
            },
        ],
        "active_probes": list(openai["active_probes"]),
    }


def _expand_path(value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return str(Path(expanded))


def normalize_config(
    raw: dict[str, Any] | None,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    defaults = default_config(home)
    config = dict(defaults)
    if raw:
        config.update(raw)
        configured_probes = raw.get("active_probes")
        if isinstance(configured_probes, list):
            merged = list(configured_probes)
            names = {
                str(item.get("name"))
                for item in merged
                if isinstance(item, dict) and item.get("name")
            }
            merged.extend(
                item for item in defaults["active_probes"] if str(item.get("name")) not in names
            )
            config["active_probes"] = merged
    raw_providers = raw.get("providers") if raw else None
    providers = normalize_providers(raw_providers)
    openai_override: dict[str, Any] = {}
    legacy_mapping = {
        "group_name": "group_name",
        "ai_domain_suffixes": "domain_suffixes",
        "ai_exact_domains": "exact_domains",
        "active_probes": "active_probes",
        "required_probe_names": "required_probe_names",
        "browser_ambiguous_probe_names": "browser_ambiguous_probe_names",
        "candidate_preflight_url": "candidate_preflight_url",
        "candidate_preflight_expected_status": "candidate_preflight_expected_status",
    }
    if raw:
        for legacy_key, provider_key in legacy_mapping.items():
            if legacy_key in raw:
                openai_override[provider_key] = config[legacy_key]
    if openai_override:
        providers[DEFAULT_PROVIDER_ID].update(openai_override)
        providers = normalize_providers(providers)
    config["providers"] = providers
    default_provider_id = str(config.get("default_provider_id") or DEFAULT_PROVIDER_ID)
    if default_provider_id not in providers:
        raise ConfigError("default_provider_unknown")
    config["default_provider_id"] = default_provider_id
    default_provider = providers[default_provider_id]
    config["group_name"] = default_provider["group_name"]
    config["ai_domain_suffixes"] = list(default_provider["domain_suffixes"])
    config["ai_exact_domains"] = list(default_provider["exact_domains"])
    config["active_probes"] = list(default_provider["active_probes"])
    config["required_probe_names"] = list(default_provider["required_probe_names"])
    config["browser_ambiguous_probe_names"] = list(
        default_provider["browser_ambiguous_probe_names"]
    )
    config["candidate_preflight_url"] = default_provider["candidate_preflight_url"]
    config["candidate_preflight_expected_status"] = default_provider[
        "candidate_preflight_expected_status"
    ]
    config["config_version"] = CONFIG_VERSION
    for key in PATH_KEYS:
        value = config.get(key)
        if isinstance(value, str):
            config[key] = _expand_path(value)
    validate_config(config)
    return config


def load_config(
    path: Path | str | None = None,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    config_path = default_config_path(home) if path is None else Path(path)
    raw: dict[str, Any] | None = None
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"config_unreadable:{type(exc).__name__}") from exc
    else:
        try:
            parsed = (
                json.loads(text) if config_path.suffix.lower() == ".json" else yaml.safe_load(text)
            )
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"config_invalid:{type(exc).__name__}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("config_root_must_be_mapping")
        raw = parsed
    config = normalize_config(raw, home=home)
    # Retain the publishable/base profiles only in memory.  This lets a later
    # config rewrite avoid copying private overlay domains into config.yaml.
    config["_provider_base_profiles"] = copy.deepcopy(config["providers"])
    overlay_path = Path(str(config["provider_overlay_path"]))
    try:
        overlay = load_provider_overlay(overlay_path)
        config["providers"] = merge_provider_overlay(config["providers"], overlay)
    except ProviderError as exc:
        raise ConfigError(str(exc)) from exc
    config["provider_overlay_loaded"] = overlay_path.is_file()
    default_provider = config["providers"][config["default_provider_id"]]
    config["group_name"] = default_provider["group_name"]
    config["ai_domain_suffixes"] = list(default_provider["domain_suffixes"])
    config["ai_exact_domains"] = list(default_provider["exact_domains"])
    config["active_probes"] = list(default_provider["active_probes"])
    config["required_probe_names"] = list(default_provider["required_probe_names"])
    config["browser_ambiguous_probe_names"] = list(
        default_provider["browser_ambiguous_probe_names"]
    )
    config["candidate_preflight_url"] = default_provider["candidate_preflight_url"]
    config["candidate_preflight_expected_status"] = default_provider[
        "candidate_preflight_expected_status"
    ]
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if int(config.get("failure_rounds_before_switch", 0)) < 2:
        raise ConfigError("failure_rounds_before_switch_must_be_at_least_2")
    if int(config.get("single_target_failure_rounds_before_switch", 0)) < int(
        config["failure_rounds_before_switch"]
    ):
        raise ConfigError("single_target_failure_rounds_must_cover_fast_rounds")
    if int(config.get("single_target_observation_seconds", 0)) < 1:
        raise ConfigError("single_target_observation_seconds_must_be_positive")
    if int(config.get("fast_failover_min_distinct_targets", 0)) < 2:
        raise ConfigError("fast_failover_min_distinct_targets_must_be_at_least_2")
    interval = float(config.get("monitor_interval_seconds", 0))
    if interval < 5:
        raise ConfigError("monitor_interval_seconds_must_be_at_least_5")
    if int(config.get("failure_confirmation_min_gap_seconds", 0)) < 1:
        raise ConfigError("failure_confirmation_min_gap_seconds_must_be_positive")
    if int(config.get("hard_probe_retry_count", -1)) < 0:
        raise ConfigError("hard_probe_retry_count_must_not_be_negative")
    if float(config.get("hard_probe_retry_delay_seconds", -1)) < 0:
        raise ConfigError("hard_probe_retry_delay_seconds_must_not_be_negative")
    if int(config.get("candidate_prepare_count", 0)) < 1:
        raise ConfigError("candidate_prepare_count_must_be_positive")
    if int(config.get("candidate_prefilter_batch_size", 0)) < int(
        config["candidate_prepare_count"]
    ):
        raise ConfigError("candidate_prefilter_batch_must_cover_prepare_count")
    if int(config.get("candidate_isolated_parallelism", 0)) < 1:
        raise ConfigError("candidate_isolated_parallelism_must_be_positive")
    if int(config.get("active_preflight_batch_size", 0)) < 1:
        raise ConfigError("active_preflight_batch_size_must_be_positive")
    if int(config.get("candidate_commit_preflight_timeout_ms", 0)) < 500:
        raise ConfigError("candidate_commit_preflight_timeout_ms_must_be_at_least_500")
    if float(config.get("candidate_reverification_delay_seconds", -1)) < 0:
        raise ConfigError("candidate_reverification_delay_seconds_must_not_be_negative")
    if str(config.get("connection_drain_mode") or "") not in {
        "preserve",
        "replacement_only",
    }:
        raise ConfigError("connection_drain_mode_invalid")
    if int(config.get("candidate_validation_samples_required", 0)) < 2:
        raise ConfigError("candidate_validation_samples_required_must_be_at_least_2")
    if int(config.get("candidate_validation_min_gap_seconds", 0)) < 1:
        raise ConfigError("candidate_validation_min_gap_seconds_must_be_positive")
    if int(config.get("active_candidate_ttl_seconds", 0)) <= 0:
        raise ConfigError("active_candidate_ttl_seconds_must_be_positive")
    if int(config.get("warm_candidate_ttl_seconds", 0)) < int(
        config["active_candidate_ttl_seconds"]
    ):
        raise ConfigError("warm_candidate_ttl_seconds_must_cover_active_ttl")
    suffixes = config.get("ai_domain_suffixes")
    if not isinstance(suffixes, list) or not suffixes:
        raise ConfigError("ai_domain_suffixes_required")
    for suffix in suffixes:
        if not isinstance(suffix, str) or "/" in suffix or "*" in suffix:
            raise ConfigError("invalid_ai_domain_suffix")
    exact_domains = config.get("ai_exact_domains", [])
    if not isinstance(exact_domains, list):
        raise ConfigError("ai_exact_domains_must_be_list")
    try:
        for domain in exact_domains:
            normalize_hostname(str(domain))
        normalized_providers = normalize_providers(config.get("providers"))
    except ProviderError as exc:
        raise ConfigError(str(exc)) from exc
    default_provider_id = str(config.get("default_provider_id") or "")
    if default_provider_id not in normalized_providers:
        raise ConfigError("default_provider_unknown")
    enabled_count = sum(
        1 for profile in normalized_providers.values() if bool(profile.get("enabled"))
    )
    if enabled_count > 8:
        raise ConfigError("too_many_enabled_providers")
    proxy_url = str(config.get("mixed_proxy_url") or "")
    if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d{1,5}", proxy_url):
        raise ConfigError("mixed_proxy_url_must_be_loopback_http")
    socket_path = str(config.get("clash_socket_path") or "")
    if not socket_path.startswith("/"):
        raise ConfigError("clash_socket_path_must_be_absolute")
    if not str(config.get("group_name") or ""):
        raise ConfigError("group_name_required")
    for key in (
        "web_feedback_confirmed_ttl_seconds",
        "web_feedback_rejected_ttl_seconds",
    ):
        if int(config.get(key, 0)) <= 0:
            raise ConfigError(f"{key}_must_be_positive")


def write_config(
    path: Path | str,
    config: dict[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    serializable = dict(config)
    base_profiles = serializable.pop("_provider_base_profiles", None)
    serializable.pop("provider_overlay_loaded", None)
    if isinstance(base_profiles, dict):
        serializable["providers"] = copy.deepcopy(base_profiles)
        # These fields mirror the selected Provider at runtime.  The base
        # profiles already contain any intentional public customization.
        for key in (
            "group_name",
            "ai_domain_suffixes",
            "ai_exact_domains",
            "active_probes",
            "required_probe_names",
            "browser_ambiguous_probe_names",
            "candidate_preflight_url",
            "candidate_preflight_expected_status",
        ):
            serializable.pop(key, None)
    normalized = normalize_config(serializable)
    normalized.pop("provider_overlay_loaded", None)
    normalized.pop("_provider_base_profiles", None)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return target
