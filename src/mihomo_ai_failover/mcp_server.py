"""Local stdio MCP adapter for agent hosts.

No network listener is opened. Read-only tools are available by default;
mutating tools require both a local opt-in and an exact confirmation phrase.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from . import __version__, engine
from .config import default_config, default_config_path, load_config, write_config
from .diagnostics import diagnose_environment as diagnose_local_environment
from .installer import INSTALL_CONFIRMATION, install_local
from .profiles import (
    ROLLBACK_CONFIRMATION,
    preview_profile_integration,
    rollback_profile_integration,
)
from .service import (
    SERVICE_UNINSTALL_CONFIRMATION,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)

MUTATION_ENV = "MIHOMO_AI_FAILOVER_MCP_MUTATIONS"
CONFIG_WRITE_CONFIRMATION = "WRITE_LOCAL_CONFIG"
SERVICE_START_CONFIRMATION = "START_LAUNCH_AGENT"
SERVICE_STOP_CONFIRMATION = "STOP_LAUNCH_AGENT"
WEB_FEEDBACK_CONFIRMATION = engine.WEB_FEEDBACK_CONFIRMATION


class MCPPolicyError(RuntimeError):
    """The local MCP mutation policy rejected an action."""


def _config_path(value: str | None) -> Path:
    return default_config_path() if value is None else Path(value).expanduser()


def _load(value: str | None) -> tuple[Path, dict[str, Any]]:
    path = _config_path(value)
    return path, load_config(path)


def _mutations_allowed(config: dict[str, Any]) -> bool:
    environment = os.environ.get(MUTATION_ENV, "").strip().lower()
    return bool(config.get("mcp_allow_mutations", False)) or environment in {
        "1",
        "true",
        "yes",
    }


def _require_mutation(
    config: dict[str, Any],
    provided: str,
    expected: str,
) -> None:
    if not _mutations_allowed(config):
        raise MCPPolicyError(
            f"mutations_disabled:set {MUTATION_ENV}=1 or enable mcp_allow_mutations"
        )
    if provided != expected:
        raise MCPPolicyError(f"confirmation_required:{expected}")


def _state(config: dict[str, Any]) -> dict[str, Any]:
    return engine.load_state(Path(str(config["runtime_path"])) / "state.json")


def _redact_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "last_status",
        "consecutive_hard_failures",
        "first_failure_at",
        "failure_reasons",
        "last_switch_at",
        "backoff_until",
        "all_unavailable_episode",
        "all_unavailable_notified",
    }
    return {key: monitor.get(key) for key in sorted(allowed)}


def _status(config: dict[str, Any], *, include_node_names: bool) -> dict[str, Any]:
    code, result = engine.command_status(config, _state(config))
    output = {
        "ok": code == 0,
        "controller": result["controller"],
        "group": result["group"],
        "group_member_count": result["group_member_count"],
        "current_node_present": bool(result.get("current")),
        "current_exit": result["current_exit"],
        "monitor": _redact_monitor(result["monitor"]),
        "pools": result["pools"],
        "catalog_nodes": result["catalog_nodes"],
    }
    if include_node_names:
        output["current_node"] = result.get("current")
    return output


def _pool_details(
    config: dict[str, Any],
    *,
    include_node_names: bool,
) -> dict[str, Any]:
    state = _state(config)
    pools = state.get("pools", {})
    output: dict[str, Any] = {
        "counts": {
            "active": len(pools.get("active", [])),
            "warm": len(pools.get("warm", [])),
            "cold": len(pools.get("cold", [])),
        },
        "independent_exits": pools.get("independent_exit_count", 0),
        "duplicate_exit_groups": pools.get("duplicate_exit_groups", 0),
        "rebuilt_at": pools.get("rebuilt_at", 0),
    }
    if include_node_names:
        output["nodes"] = {key: list(pools.get(key, [])) for key in ("active", "warm", "cold")}
    return output


def _recent_events(
    config: dict[str, Any],
    limit: int,
    *,
    include_node_names: bool,
) -> list[dict[str, Any]]:
    path = Path(str(config["log_path"]))
    if not path.is_file():
        return []
    safe_limit = max(1, min(int(limit), 100))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-safe_limit:]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    name_fields = {"old_node", "new_node", "candidate", "current", "attempted"}
    allowed_fields = {
        "time",
        "event",
        "reason",
        "layer",
        "layers",
        "closed_connections",
        "verification",
        "status",
        "error",
        "reasons",
        "backoff_until",
        "notified",
    }
    if include_node_names:
        allowed_fields |= name_fields
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            events.append({key: record[key] for key in allowed_fields if key in record})
    return events


def _simulate_invariants() -> dict[str, Any]:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-simulation"))
    state = engine.default_state()
    current = engine.node_template("ss")
    current.update(
        {
            "exit_ip": "192.0.2.10",
            "asn": "AS64500",
            "openai_status": "healthy",
            "deep_verified_at": 10_000,
        }
    )
    candidate = engine.node_template("ss")
    candidate.update(
        {
            "exit_ip": "198.51.100.20",
            "asn": "AS64501",
            "openai_status": "healthy",
            "deep_verified_at": 10_000,
            "preflight_ok": True,
        }
    )
    state["nodes"] = {"current": current, "candidate": candidate}
    ordered = engine.candidate_order(state, ["candidate"], "current")
    first_failure_to_switch_upper_bound = (
        int(config["monitor_interval_seconds"])
        + int(config["candidate_preflight_timeout_ms"]) // 1000
        + int(config["switch_connection_wait_seconds"])
        + max(int(item["timeout_seconds"]) for item in config["active_probes"])
    )
    return {
        "passed": bool(
            config["failure_rounds_before_switch"] == 2
            and first_failure_to_switch_upper_bound <= 30
            and ordered == ["candidate"]
        ),
        "failure_rounds_before_switch": config["failure_rounds_before_switch"],
        "upper_bound_seconds": first_failure_to_switch_upper_bound,
        "different_exit_preferred": ordered == ["candidate"],
        "live_proxy_changed": False,
    }


def create_server() -> Any:
    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised by base-only installs
        raise RuntimeError(
            'MCP support is not installed; install "mihomo-ai-failover[mcp]"'
        ) from exc

    server = MCPServer(
        name="mihomo-ai-failover",
        title="Mihomo AI Failover",
        description="Diagnose and operate OpenAI-aware failover on the local Mac.",
        version=__version__,
        instructions=(
            "Call diagnose_environment before proposing changes. Keep read-only "
            "tools as the default. Never infer a node failure from a silent Codex "
            "UI alone. Mutating tools require local opt-in and exact confirmation. "
            "Do not expose controller secrets, subscriptions, proxy credentials, "
            "or inventories. This server controls only the dedicated AI group."
        ),
    )

    read_local = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    read_network = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    write_local = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    destructive_local = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        title="Diagnose local Mihomo environment",
        description=(
            "Inspect macOS, Clash Verge paths, Unix controller reachability, "
            "system proxy state, AI group presence, and LaunchAgent status. "
            "Use before any installation or troubleshooting action."
        ),
        annotations=read_local,
    )
    def diagnose_environment(config_path: str | None = None) -> dict[str, Any]:
        _, config = _load(config_path)
        return diagnose_local_environment(config)

    @server.tool(
        title="Get failover status",
        description=(
            "Return controller, dedicated AI group, health state, and pool counts. "
            "Node names remain hidden unless include_node_names is true."
        ),
        annotations=read_local,
    )
    def get_status(
        config_path: str | None = None,
        include_node_names: bool = False,
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        return _status(config, include_node_names=include_node_names)

    @server.tool(
        title="Run read-only OpenAI health check",
        description=(
            "Probe direct connectivity and the real OpenAI API/auth/web path "
            "through the selected AI group. This never switches nodes."
        ),
        annotations=read_network,
    )
    def run_health_check(
        config_path: str | None = None,
        include_node_name: bool = False,
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        code, result = engine.command_check(config)
        if not include_node_name:
            result.pop("current", None)
        return {"ok": code == 0, **result}

    @server.tool(
        title="List failover pools",
        description=(
            "Return active, warm, and cold pool counts plus independent-exit "
            "statistics. Node names are opt-in; exit IPs are never returned."
        ),
        annotations=read_local,
    )
    def list_pools(
        config_path: str | None = None,
        include_node_names: bool = False,
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        return _pool_details(config, include_node_names=include_node_names)

    @server.tool(
        title="Read recent sanitized failover events",
        description=(
            "Read up to 100 local sanitized events. Credentials, subscriptions, "
            "server addresses, and exit IPs are never returned."
        ),
        annotations=read_local,
    )
    def get_recent_events(
        config_path: str | None = None,
        limit: int = 20,
        include_node_names: bool = False,
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        return {
            "events": _recent_events(
                config,
                limit,
                include_node_names=include_node_names,
            )
        }

    @server.tool(
        title="Preview local installation",
        description=(
            "Preview persistent Clash Verge group/rule changes and report the "
            "environment without writing files."
        ),
        annotations=read_local,
    )
    def preview_install(config_path: str | None = None) -> dict[str, Any]:
        _, config = _load(config_path)
        try:
            profile = preview_profile_integration(
                config["clash_data_root"],
                group_name=config["group_name"],
                suffixes=list(config["ai_domain_suffixes"]),
            )
        except Exception as exc:
            profile = {"ok": False, "error": str(exc) or type(exc).__name__}
        return {
            "diagnosis": diagnose_local_environment(config),
            "profile": profile,
            "mutations_enabled": _mutations_allowed(config),
        }

    @server.tool(
        title="Simulate failover invariants",
        description=(
            "Run an isolated deterministic simulation of the two-failure timing "
            "and distinct-exit ranking invariants. No live proxy is changed."
        ),
        annotations=read_local,
    )
    def simulate_failover() -> dict[str, Any]:
        return _simulate_invariants()

    @server.tool(
        title="Write local failover config",
        description=(
            "Write an auto-discovered local config. Requires mutation opt-in and "
            f"confirmation={CONFIG_WRITE_CONFIRMATION}."
        ),
        annotations=write_local,
    )
    def initialize_config(
        config_path: str | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        path = _config_path(config_path)
        config = load_config(path)
        _require_mutation(config, confirmation, CONFIG_WRITE_CONFIRMATION)
        target = write_config(path, config)
        return {"created": True, "config": str(target)}

    @server.tool(
        title="Record real-browser OpenAI web feedback",
        description=(
            "Record time-limited ChatGPT browser login evidence for the node's "
            "current observed exit fingerprint. status must be confirmed or "
            "rejected. This never switches a proxy and requires the monitor to "
            "be stopped, mutation opt-in, and "
            f"confirmation={WEB_FEEDBACK_CONFIRMATION}."
        ),
        annotations=write_local,
    )
    def record_web_feedback(
        node: str,
        status: str,
        reason: str,
        config_path: str | None = None,
        ttl_seconds: int | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        _require_mutation(config, confirmation, WEB_FEEDBACK_CONFIRMATION)
        state_path, lock_path, log_path = engine.ensure_runtime(config)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MCPPolicyError("monitor_must_be_stopped") from exc
            state = engine.load_state(state_path)
            _, result = engine.command_web_feedback(
                config,
                state,
                state_path,
                log_path,
                node,
                status,
                reason,
                ttl_seconds,
            )
        result.pop("node", None)
        return {"ok": True, **result}

    @server.tool(
        title="Install local failover",
        description=(
            "Back up and integrate the persistent AI group/rules, then install "
            "the LaunchAgent. Requires mutation opt-in and "
            f"confirmation={INSTALL_CONFIRMATION}."
        ),
        annotations=write_local,
    )
    def install_failover(
        config_path: str | None = None,
        confirmation: str = "",
        start_when_ready: bool = False,
    ) -> dict[str, Any]:
        path, config = _load(config_path)
        _require_mutation(config, confirmation, INSTALL_CONFIRMATION)
        return install_local(
            path,
            confirmation=INSTALL_CONFIRMATION,
            start=start_when_ready,
        )

    @server.tool(
        title="Start failover LaunchAgent",
        description=(
            "Start or restart the installed monitor. Requires mutation opt-in "
            f"and confirmation={SERVICE_START_CONFIRMATION}."
        ),
        annotations=write_local,
    )
    def start_monitor(
        config_path: str | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        _require_mutation(config, confirmation, SERVICE_START_CONFIRMATION)
        return start_service()

    @server.tool(
        title="Stop failover LaunchAgent",
        description=(
            "Stop the monitor without changing Clash Verge. Requires mutation "
            f"opt-in and confirmation={SERVICE_STOP_CONFIRMATION}."
        ),
        annotations=write_local,
    )
    def stop_monitor(
        config_path: str | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        _require_mutation(config, confirmation, SERVICE_STOP_CONFIRMATION)
        return stop_service()

    @server.tool(
        title="Roll back persistent profile integration",
        description=(
            "Restore the latest backed-up Clash Verge enhancement files. "
            f"Requires mutation opt-in and confirmation={ROLLBACK_CONFIRMATION}."
        ),
        annotations=destructive_local,
    )
    def rollback_profile(
        config_path: str | None = None,
        confirmation: str = "",
        backup_path: str | None = None,
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        _require_mutation(config, confirmation, ROLLBACK_CONFIRMATION)
        return rollback_profile_integration(
            config["runtime_path"],
            confirmation=ROLLBACK_CONFIRMATION,
            backup_path=backup_path,
            expected_clash_root=config["clash_data_root"],
        )

    @server.tool(
        title="Uninstall failover LaunchAgent",
        description=(
            "Stop the monitor and move its plist to Trash. Persistent Clash "
            "profile changes are not removed. Requires mutation opt-in and "
            f"confirmation={SERVICE_UNINSTALL_CONFIRMATION}."
        ),
        annotations=destructive_local,
    )
    def uninstall_monitor(
        config_path: str | None = None,
        confirmation: str = "",
    ) -> dict[str, Any]:
        _, config = _load(config_path)
        _require_mutation(config, confirmation, SERVICE_UNINSTALL_CONFIRMATION)
        return uninstall_service(confirmation=SERVICE_UNINSTALL_CONFIRMATION)

    @server.tool(
        title="Get LaunchAgent status",
        description="Return whether the per-user failover LaunchAgent is installed and running.",
        annotations=read_local,
    )
    def get_service_status() -> dict[str, Any]:
        return service_status()

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
