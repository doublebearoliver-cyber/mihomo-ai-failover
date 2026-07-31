"""macOS LaunchAgent lifecycle management."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SERVICE_LABEL = "io.github.doublebearoliver.mihomo-ai-failover"
SERVICE_INSTALL_CONFIRMATION = "INSTALL_LAUNCH_AGENT"
SERVICE_UNINSTALL_CONFIRMATION = "UNINSTALL_LAUNCH_AGENT"


class ServiceError(RuntimeError):
    """LaunchAgent operation failed."""


def launch_agent_path(home: Path | None = None) -> Path:
    base = Path.home() if home is None else Path(home)
    return base / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _service_target() -> str:
    return f"gui/{os.getuid()}/{SERVICE_LABEL}"


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _program_arguments(
    config_path: Path,
    executable: Path | None = None,
) -> list[str]:
    if executable is not None:
        return [str(executable), "daemon", "--config", str(config_path)]
    discovered = shutil.which("mihomo-ai-failover")
    if discovered:
        return [str(Path(discovered).resolve()), "daemon", "--config", str(config_path)]
    return [
        str(Path(sys.executable).resolve()),
        "-m",
        "mihomo_ai_failover",
        "daemon",
        "--config",
        str(config_path),
    ]


def build_launch_agent(
    config_path: Path,
    log_directory: Path,
    *,
    executable: Path | None = None,
) -> dict[str, Any]:
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _program_arguments(config_path, executable),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_directory / "launchd-stdout.log"),
        "StandardErrorPath": str(log_directory / "launchd-stderr.log"),
        "EnvironmentVariables": {
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
    }


def service_status(home: Path | None = None) -> dict[str, Any]:
    plist_path = launch_agent_path(home)
    if sys.platform != "darwin":
        return {
            "supported": False,
            "installed": plist_path.exists(),
            "running": False,
        }
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "print", _service_target()],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "supported": True,
            "installed": plist_path.exists(),
            "running": False,
            "available": False,
        }
    return {
        "supported": True,
        "installed": plist_path.exists(),
        "running": completed.returncode == 0,
        "available": True,
        "label": SERVICE_LABEL,
        "plist": str(plist_path),
    }


def install_service(
    config_path: Path | str,
    log_directory: Path | str,
    *,
    confirmation: str,
    executable: Path | None = None,
    start: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    if confirmation != SERVICE_INSTALL_CONFIRMATION:
        raise ServiceError("explicit_confirmation_required")
    if sys.platform != "darwin":
        raise ServiceError("macos_required")
    plist_path = launch_agent_path(home)
    plist_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = Path(log_directory)
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = build_launch_agent(Path(config_path), logs, executable=executable)
    payload = plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)
    backup = None
    if plist_path.exists():
        backup = plist_path.with_name(f"{plist_path.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(plist_path, backup)
    temporary = plist_path.with_name(f".{plist_path.name}.tmp")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, plist_path)
    result = {
        "installed": True,
        "started": False,
        "plist": str(plist_path),
        "backup": str(backup) if backup else None,
        "program_arguments": document["ProgramArguments"],
    }
    if start:
        start_result = start_service(home)
        result["started"] = bool(start_result["running"])
    return result


def start_service(home: Path | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise ServiceError("macos_required")
    plist_path = launch_agent_path(home)
    if not plist_path.is_file():
        raise ServiceError("launch_agent_not_installed")
    subprocess.run(
        ["/bin/launchctl", "bootout", _service_target()],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
    )
    completed = subprocess.run(
        ["/bin/launchctl", "bootstrap", _launch_domain(), str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise ServiceError("launchctl_bootstrap_failed")
    return service_status(home)


def stop_service(home: Path | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise ServiceError("macos_required")
    subprocess.run(
        ["/bin/launchctl", "bootout", _service_target()],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    return service_status(home)


def uninstall_service(
    *,
    confirmation: str,
    home: Path | None = None,
) -> dict[str, Any]:
    if confirmation != SERVICE_UNINSTALL_CONFIRMATION:
        raise ServiceError("explicit_confirmation_required")
    stop_service(home)
    plist_path = launch_agent_path(home)
    if not plist_path.exists():
        return {"uninstalled": True, "recoverable_at": None}
    base = Path.home() if home is None else Path(home)
    trash = base / ".Trash"
    trash.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = trash / f"{plist_path.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(plist_path), str(destination))
    return {"uninstalled": True, "recoverable_at": str(destination)}
