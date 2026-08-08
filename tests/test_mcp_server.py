from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from mihomo_ai_failover import engine
from mihomo_ai_failover.config import default_config, write_config
from mihomo_ai_failover.mcp_server import (
    MUTATION_ENV,
    WEB_FEEDBACK_CONFIRMATION,
    create_server,
)


@pytest.mark.asyncio
async def test_mcp_contract_and_annotations() -> None:
    async with Client(create_server()) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert {
        "diagnose_environment",
        "list_provider_profiles",
        "check_provider_paths",
        "discover_provider_domains",
        "preview_provider_overlay",
        "apply_provider_overlay",
        "get_status",
        "run_health_check",
        "list_pools",
        "get_recent_events",
        "preview_install",
        "simulate_failover",
        "initialize_config",
        "install_failover",
        "start_monitor",
        "stop_monitor",
        "record_web_feedback",
        "rollback_profile",
        "uninstall_monitor",
        "get_service_status",
    } == set(tools)
    assert tools["diagnose_environment"].annotations.read_only_hint is True
    assert tools["discover_provider_domains"].annotations.read_only_hint is True
    assert tools["apply_provider_overlay"].annotations.read_only_hint is False
    assert tools["run_health_check"].annotations.open_world_hint is True
    assert tools["install_failover"].annotations.read_only_hint is False
    assert tools["record_web_feedback"].annotations.read_only_hint is False
    assert tools["rollback_profile"].annotations.destructive_hint is True


@pytest.mark.asyncio
async def test_mcp_simulation_is_isolated_and_passes() -> None:
    async with Client(create_server()) as client:
        result = await client.call_tool("simulate_failover", {})

    assert result.is_error is False
    assert result.structured_content["passed"] is True
    assert result.structured_content["failure_rounds_before_switch"] == 2
    assert result.structured_content["upper_bound_seconds"] <= 30
    assert result.structured_content["upper_bound_scope"] == "prepared_retry_free_candidate"
    assert result.structured_content["prepared_candidate_target"] == 3
    assert result.structured_content["live_selection_budget"] == 2
    assert result.structured_content["different_exit_preferred"] is True
    assert result.structured_content["live_proxy_changed"] is False


@pytest.mark.asyncio
async def test_mcp_mutation_is_rejected_by_default(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    async with Client(create_server()) as client:
        result = await client.call_tool(
            "initialize_config",
            {
                "config_path": str(target),
                "confirmation": "WRITE_LOCAL_CONFIG",
            },
        )

    assert result.is_error is True
    assert not target.exists()
    assert "mutations_disabled" in " ".join(
        block.text for block in result.content if hasattr(block, "text")
    )


@pytest.mark.asyncio
async def test_mcp_records_browser_feedback_without_returning_exit_ip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)
    runtime = Path(config["runtime_path"])
    runtime.mkdir(parents=True)
    state = engine.default_state()
    entry = engine.node_template("ss")
    entry.update(
        {
            "exit_ip": "192.0.2.10",
            "exit_country": "CH",
            "asn": "AS64500",
            "openai_status": "healthy",
            "deep_verified_at": engine.now_ts(),
        }
    )
    state["nodes"] = {"瑞士出口": entry}
    engine.atomic_write_json(runtime / "state.json", state)
    monkeypatch.setenv(MUTATION_ENV, "1")

    async with Client(create_server()) as client:
        result = await client.call_tool(
            "record_web_feedback",
            {
                "node": "瑞士出口",
                "status": "confirmed",
                "reason": "browser_login_success",
                "config_path": str(config_path),
                "confirmation": WEB_FEEDBACK_CONFIRMATION,
            },
        )

    assert result.is_error is False
    assert result.structured_content["web_feedback"] == "confirmed"
    assert "192.0.2.10" not in str(result.structured_content)
