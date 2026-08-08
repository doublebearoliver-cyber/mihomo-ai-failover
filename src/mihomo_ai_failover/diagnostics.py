"""Read-only environment diagnostics with secret-safe output."""

from __future__ import annotations

import os
import platform
import stat
import subprocess
from pathlib import Path
from typing import Any

from . import engine
from .service import service_status


def _path_report(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    try:
        mode = path.stat().st_mode
        result["kind"] = (
            "socket"
            if stat.S_ISSOCK(mode)
            else "file"
            if stat.S_ISREG(mode)
            else "directory"
            if stat.S_ISDIR(mode)
            else "other"
        )
    except OSError:
        result["kind"] = "unavailable"
    return result


def _system_proxy() -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"supported": False}
    try:
        completed = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"supported": True, "available": False}
    text = completed.stdout

    def value(key: str) -> str | None:
        marker = f"{key} :"
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(marker):
                return stripped.split(":", 1)[1].strip()
        return None

    return {
        "supported": True,
        "available": completed.returncode == 0,
        "http_enabled": value("HTTPEnable") == "1",
        "http_host": value("HTTPProxy"),
        "http_port": value("HTTPPort"),
        "https_enabled": value("HTTPSEnable") == "1",
        "https_host": value("HTTPSProxy"),
        "https_port": value("HTTPSPort"),
        "socks_enabled": value("SOCKSEnable") == "1",
        "socks_host": value("SOCKSProxy"),
        "socks_port": value("SOCKSPort"),
    }


def diagnose_environment(config: dict[str, Any]) -> dict[str, Any]:
    config_path = str(config["clash_config_path"])
    socket_path = str(config["clash_socket_path"])
    controller: dict[str, Any] = {
        "transport": "unix",
        "socket": _path_report(socket_path),
        "secret_present": False,
        "reachable": False,
        "version": None,
        "ai_group_present": False,
    }
    try:
        secret = engine.read_secret(config_path)
        controller["secret_present"] = bool(secret)
        client = engine.ClashController(socket_path, secret)
        version = client.version()
        controller["reachable"] = True
        controller["version"] = str(version.get("version") or "")[:80]
        try:
            group = client.proxy(str(config["group_name"]))
        except engine.ControllerError:
            group = {}
        controller["ai_group_present"] = bool(isinstance(group, dict) and group.get("now"))
        controller["ai_group_member_count"] = (
            len(group.get("all", [])) if isinstance(group, dict) else 0
        )
        provider_groups: list[dict[str, Any]] = []
        providers = config.get("providers", {})
        if isinstance(providers, dict):
            for provider_id, profile in providers.items():
                if not isinstance(profile, dict) or not bool(profile.get("enabled")):
                    continue
                try:
                    provider_group = client.proxy(str(profile.get("group_name") or ""))
                except engine.ControllerError:
                    provider_group = {}
                provider_groups.append(
                    {
                        "provider_id": str(provider_id),
                        "display_name": str(profile.get("display_name") or provider_id),
                        "present": bool(
                            isinstance(provider_group, dict) and provider_group.get("now")
                        ),
                        "member_count": (
                            len(provider_group.get("all", []))
                            if isinstance(provider_group, dict)
                            else 0
                        ),
                    }
                )
        controller["provider_groups"] = provider_groups
    except (engine.ControllerError, OSError) as exc:
        controller["error"] = type(exc).__name__

    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "supported": platform.system() == "Darwin",
        },
        "clash": {
            "data_root": _path_report(str(config["clash_data_root"])),
            "generated_config": _path_report(config_path),
            "mihomo_core": _path_report(str(config["mihomo_core_path"])),
            "controller": controller,
        },
        "system_proxy": _system_proxy(),
        "service": service_status(),
        "config_safety": {
            "controller_is_unix_socket": os.path.isabs(socket_path),
            "mixed_proxy_is_loopback": str(config["mixed_proxy_url"]).startswith(
                ("http://127.0.0.1:", "http://localhost:")
            ),
            "mcp_mutations_enabled": bool(config.get("mcp_allow_mutations", False)),
            "provider_overlay_loaded": bool(config.get("provider_overlay_loaded", False)),
        },
    }
