"""Configuration discovery and normalization.

The public project never embeds a user's home directory or controller secret.
Clash Verge paths are discovered at runtime and remain overridable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

APP_DIR_NAME = "Mihomo AI Failover"
CLASH_VERGE_APP_ID = "io.github.clash-verge-rev.clash-verge-rev"
DEFAULT_GROUP_NAME = "🤖 AI稳定出口"
DEFAULT_SOCKET_PATH = "/tmp/verge/verge-mihomo.sock"
DEFAULT_CONFIG_FILENAME = "config.yaml"
CONFIG_VERSION = 1

DEFAULT_AI_SUFFIXES = [
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "oaistatsig.com",
]

PATH_KEYS = {
    "clash_data_root",
    "clash_config_path",
    "clash_generated_config_path",
    "clash_socket_path",
    "runtime_path",
    "log_path",
    "mihomo_core_path",
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

    return {
        "config_version": CONFIG_VERSION,
        "clash_data_root": str(root),
        "clash_config_path": str(generated),
        "clash_generated_config_path": str(generated),
        "clash_socket_path": socket_path,
        "runtime_path": str(runtime),
        "log_path": str(log_dir / "monitor.jsonl"),
        "mixed_proxy_url": f"http://127.0.0.1:{mixed_port}",
        "group_name": DEFAULT_GROUP_NAME,
        "mihomo_core_path": str(discover_mihomo_core()),
        "ai_domain_suffixes": list(DEFAULT_AI_SUFFIXES),
        "node_exclude_regex": (
            r"(套餐|流量|到期|过期|剩余|官网|网址|更新|订阅|倍率|公告|"
            r"DIRECT|REJECT|PASS)"
        ),
        "monitor_interval_seconds": 10,
        "failure_rounds_before_switch": 2,
        "switch_connection_wait_seconds": 3,
        "max_candidate_attempts_per_failover": 3,
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
        "candidate_preflight_url": "https://api.openai.com/v1/models",
        "candidate_preflight_expected_status": "401",
        "candidate_preflight_timeout_ms": 4000,
        "candidate_concurrency": 20,
        "active_preflight_interval_seconds": 10,
        "warm_scan_interval_seconds": 300,
        "cold_scan_interval_seconds": 21600,
        "cold_scan_batch_size": 5,
        "catalog_refresh_interval_seconds": 300,
        "initial_deep_scan_max": 80,
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
        ],
    }


def _expand_path(value: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(value))
    return str(Path(expanded))


def normalize_config(
    raw: dict[str, Any] | None,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    config = default_config(home)
    if raw:
        config.update(raw)
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
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return normalize_config(None, home=home)
    except OSError as exc:
        raise ConfigError(f"config_unreadable:{type(exc).__name__}") from exc
    try:
        raw = json.loads(text) if config_path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"config_invalid:{type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config_root_must_be_mapping")
    return normalize_config(raw, home=home)


def validate_config(config: dict[str, Any]) -> None:
    if int(config.get("failure_rounds_before_switch", 0)) < 2:
        raise ConfigError("failure_rounds_before_switch_must_be_at_least_2")
    interval = float(config.get("monitor_interval_seconds", 0))
    if interval < 5:
        raise ConfigError("monitor_interval_seconds_must_be_at_least_5")
    suffixes = config.get("ai_domain_suffixes")
    if not isinstance(suffixes, list) or not suffixes:
        raise ConfigError("ai_domain_suffixes_required")
    for suffix in suffixes:
        if not isinstance(suffix, str) or "/" in suffix or "*" in suffix:
            raise ConfigError("invalid_ai_domain_suffix")
    proxy_url = str(config.get("mixed_proxy_url") or "")
    if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d{1,5}", proxy_url):
        raise ConfigError("mixed_proxy_url_must_be_loopback_http")
    socket_path = str(config.get("clash_socket_path") or "")
    if not socket_path.startswith("/"):
        raise ConfigError("clash_socket_path_must_be_absolute")
    if not str(config.get("group_name") or ""):
        raise ConfigError("group_name_required")


def write_config(
    path: Path | str,
    config: dict[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    target = Path(path)
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    normalized = normalize_config(config)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return target
