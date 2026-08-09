from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from mihomo_ai_failover import cli, engine
from mihomo_ai_failover.config import default_config, write_config


def test_cli_reports_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert "mihomo-ai-failover 0.2.2" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["status", "check"])
def test_read_only_core_commands_do_not_create_runtime(
    tmp_path: Path,
    command: str,
) -> None:
    runtime = tmp_path / "runtime-that-must-not-exist"
    logs = tmp_path / "logs-that-must-not-exist" / "monitor.jsonl"
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    config["runtime_path"] = str(runtime)
    config["log_path"] = str(logs)
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)

    if command == "status":
        patch = mock.patch.object(
            engine,
            "command_status",
            return_value=(0, {"controller": "test"}),
        )
    else:
        patch = mock.patch.object(
            engine,
            "command_check",
            return_value=(0, {"route": "test"}),
        )

    with patch:
        assert engine.main([command, "--config", str(config_path)]) == 0

    assert not runtime.exists()
    assert not logs.parent.exists()


def test_provider_overlay_preview_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)
    overlay = Path(config["provider_overlay_path"])

    result = cli.main(
        [
            "provider-overlay-preview",
            "--config",
            str(config_path),
            "--provider",
            "kimi",
            "--domain",
            "api.kimi-service.example",
            "--enable",
        ]
    )

    assert result == 0
    assert not overlay.exists()
    assert '"provider_id": "kimi"' in capsys.readouterr().out
