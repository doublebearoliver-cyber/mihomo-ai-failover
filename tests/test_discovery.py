from __future__ import annotations

from pathlib import Path

import pytest

from mihomo_ai_failover import discovery
from mihomo_ai_failover.config import default_config, load_config, write_config
from mihomo_ai_failover.discovery import (
    PROVIDER_OVERLAY_CONFIRMATION,
    apply_provider_overlay,
    observe_provider_connections,
    preview_provider_overlay,
)
from mihomo_ai_failover.providers import ProviderError


def _connection(host: str, process: str = "") -> dict[str, object]:
    return {"metadata": {"host": host, "processPath": process}}


class SnapshotController:
    def __init__(self, snapshots: list[list[dict[str, object]]]):
        self.snapshots = snapshots
        self.index = 0

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, object]:
        del method, path, payload, timeout
        selected = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return {"connections": selected}


def test_observation_uses_known_and_process_evidence_without_leaking_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    controller = SnapshotController(
        [
            [_connection("unrelated.example", "/Applications/Safari.app/Safari")],
            [
                _connection("api.workbuddy.cn", "/Applications/WorkBuddy.app/WorkBuddy"),
                _connection(
                    "api.workbuddy-helper.example",
                    "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy",
                ),
                _connection(
                    "api.googleapis.com",
                    "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy",
                ),
                _connection("new-temporal.example", "/Applications/Safari.app/Safari"),
            ],
        ]
    )
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))

    result = observe_provider_connections(
        controller,
        config,
        "workbuddy-cn",
        duration_seconds=1,
    )

    by_host = {item["host"]: item for item in result["candidates"]}
    assert by_host["api.workbuddy.cn"]["confidence"] == "high"
    assert by_host["api.workbuddy-helper.example"]["recommended_exact_domain"] is True
    assert by_host["api.googleapis.com"]["shared_infrastructure"] is True
    assert by_host["api.googleapis.com"]["recommended_exact_domain"] is False
    assert "new-temporal.example" not in by_host
    assert "/Applications" not in str(result)


def test_temporal_only_candidates_are_visible_only_when_explicitly_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    controller = SnapshotController([[], [_connection("ephemeral.example")]])
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))

    result = observe_provider_connections(
        controller,
        config,
        "kimi",
        duration_seconds=1,
        include_temporal_candidates=True,
    )

    assert result["candidates"][0]["evidence"] == "temporal_only"
    assert result["candidates"][0]["recommended_exact_domain"] is False


def test_overlay_preview_adds_narrow_routing_and_critical_transport_probe(
    tmp_path: Path,
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    result = preview_provider_overlay(
        config,
        "kimi",
        exact_domains=["api.kimi-service.example"],
        critical_domains=["stream.kimi-service.example"],
        enabled=True,
    )

    override = result["provider_override"]
    assert override["enabled"] is True
    assert override["exact_domains"] == [
        "api.kimi-service.example",
        "stream.kimi-service.example",
    ]
    added = [
        item
        for item in override["active_probes"]
        if item["url"] == "https://stream.kimi-service.example/"
    ]
    assert len(added) == 1
    assert added[0]["kind"] == "generic_transport"
    assert added[0]["name"] in override["required_probe_names"]
    assert result["requires_profile_reinstall"] is True
    assert result["requires_monitor_restart"] is True


def test_probe_only_change_does_not_claim_clash_profile_restart(tmp_path: Path) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    result = preview_provider_overlay(
        config,
        "kimi",
        exact_domains=[],
        critical_domains=["stream.kimi.com"],
        enabled=None,
    )

    assert result["requires_profile_reinstall"] is False
    assert result["requires_clash_restart"] is False
    assert result["requires_monitor_restart"] is True


def test_shared_infrastructure_cannot_drive_provider_failover(tmp_path: Path) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    with pytest.raises(ProviderError, match="shared_infrastructure"):
        preview_provider_overlay(
            config,
            "kimi",
            exact_domains=[],
            critical_domains=["api.googleapis.com"],
            enabled=True,
        )


def test_overlay_apply_is_guarded_private_and_loadable_without_main_config(
    tmp_path: Path,
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    with pytest.raises(ProviderError, match="explicit_confirmation_required"):
        apply_provider_overlay(
            config,
            "mavis",
            exact_domains=["api.mavis-service.example"],
            enabled=True,
            confirmation="yes",
        )

    result = apply_provider_overlay(
        config,
        "mavis",
        exact_domains=["api.mavis-service.example"],
        enabled=True,
        confirmation=PROVIDER_OVERLAY_CONFIRMATION,
    )

    target = Path(result["overlay_path"])
    assert target.is_file()
    assert target.stat().st_mode & 0o077 == 0
    loaded = load_config(tmp_path / "missing-config.yaml", home=tmp_path)
    assert loaded["provider_overlay_loaded"] is True
    assert loaded["providers"]["mavis"]["enabled"] is True
    assert loaded["providers"]["mavis"]["exact_domains"] == ["api.mavis-service.example"]


def test_rewriting_loaded_config_does_not_embed_private_overlay(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    write_config(config_path, config)
    apply_provider_overlay(
        config,
        "kimi",
        exact_domains=["private-api.kimi-service.example"],
        enabled=True,
        confirmation=PROVIDER_OVERLAY_CONFIRMATION,
    )
    loaded = load_config(config_path, home=tmp_path)
    rewritten = tmp_path / "rewritten.yaml"

    write_config(rewritten, loaded)

    assert "private-api.kimi-service.example" not in rewritten.read_text(encoding="utf-8")
