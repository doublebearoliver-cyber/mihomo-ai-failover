from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from mihomo_ai_failover.mcp_server import create_server


@pytest.mark.asyncio
async def test_mcp_contract_and_annotations() -> None:
    async with Client(create_server()) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert {
        "diagnose_environment",
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
        "rollback_profile",
        "uninstall_monitor",
        "get_service_status",
    } == set(tools)
    assert tools["diagnose_environment"].annotations.read_only_hint is True
    assert tools["run_health_check"].annotations.open_world_hint is True
    assert tools["install_failover"].annotations.read_only_hint is False
    assert tools["rollback_profile"].annotations.destructive_hint is True


@pytest.mark.asyncio
async def test_mcp_simulation_is_isolated_and_passes() -> None:
    async with Client(create_server()) as client:
        result = await client.call_tool("simulate_failover", {})

    assert result.is_error is False
    assert result.structured_content["passed"] is True
    assert result.structured_content["failure_rounds_before_switch"] == 2
    assert result.structured_content["upper_bound_seconds"] <= 30
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
