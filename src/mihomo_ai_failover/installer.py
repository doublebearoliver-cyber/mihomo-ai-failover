"""High-level local installation orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ConfigError, default_config, load_config, write_config
from .profiles import PROFILE_CONFIRMATION, apply_profile_integration
from .providers import routing_profiles
from .service import SERVICE_INSTALL_CONFIRMATION, install_service

INSTALL_CONFIRMATION = "INSTALL_MIHOMO_AI_FAILOVER"


def install_local(
    config_path: Path | str,
    *,
    confirmation: str,
    start: bool = False,
    service_home: Path | None = None,
    service_executable: Path | None = None,
) -> dict[str, Any]:
    """Install persistent profile enhancements and the user LaunchAgent.

    The LaunchAgent is not started in the same call when the profile changed,
    because Clash Verge must regenerate its runtime config first.
    """
    if confirmation != INSTALL_CONFIRMATION:
        raise ConfigError("explicit_confirmation_required")
    target = Path(config_path)
    if target.exists():
        config = load_config(target)
        config_created = False
    else:
        config = default_config()
        write_config(target, config)
        config = load_config(target)
        config_created = True
    profile = apply_profile_integration(
        config["clash_data_root"],
        config["runtime_path"],
        confirmation=PROFILE_CONFIRMATION,
        group_name=config["group_name"],
        suffixes=list(config["ai_domain_suffixes"]),
        exact_domains=list(config.get("ai_exact_domains", [])),
        provider_profiles=routing_profiles(config),
    )
    can_start = bool(start and not profile["restart_required"])
    service = install_service(
        target,
        Path(str(config["log_path"])).parent,
        confirmation=SERVICE_INSTALL_CONFIRMATION,
        executable=service_executable,
        start=can_start,
        home=service_home,
    )
    return {
        "installed": True,
        "config_created": config_created,
        "config": str(target),
        "profile": profile,
        "service": service,
        "next_action": (
            "Restart Clash Verge, then start the LaunchAgent"
            if profile["restart_required"]
            else "LaunchAgent is ready"
        ),
    }
