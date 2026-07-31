from __future__ import annotations

from pathlib import Path
from unittest import mock

import yaml

from mihomo_ai_failover import installer, service
from mihomo_ai_failover.config import default_config, write_config
from mihomo_ai_failover.profiles import (
    ROLLBACK_CONFIRMATION,
    rollback_profile_integration,
)


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_full_install_isolated_then_profile_rollback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    clash_root = home / "clash"
    runtime = home / "runtime"
    log_path = home / "logs" / "monitor.jsonl"
    config_path = home / "config.yaml"
    executable = home / "bin" / "mihomo-ai-failover"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    original_profiles = {
        "current": "subscription",
        "items": [
            {
                "uid": "subscription",
                "type": "remote",
                "name": "Example subscription",
                "file": "subscription.yaml",
                "updated": 1,
                "option": {},
            }
        ],
    }
    _write_yaml(clash_root / "profiles.yaml", original_profiles)
    config = default_config(home=home, clash_root=clash_root)
    config["runtime_path"] = str(runtime)
    config["log_path"] = str(log_path)
    write_config(config_path, config)

    with mock.patch.object(service.sys, "platform", "darwin"):
        result = installer.install_local(
            config_path,
            confirmation=installer.INSTALL_CONFIRMATION,
            service_home=home,
            service_executable=executable,
        )

    assert result["installed"] is True
    assert result["profile"]["changed"] is True
    assert result["profile"]["restart_required"] is True
    assert result["service"]["started"] is False
    assert Path(result["service"]["plist"]).is_file()
    assert result["next_action"].startswith("Restart Clash Verge")

    rollback_profile_integration(
        runtime,
        confirmation=ROLLBACK_CONFIRMATION,
        expected_clash_root=clash_root,
    )
    restored = yaml.safe_load((clash_root / "profiles.yaml").read_text(encoding="utf-8"))
    assert restored == original_profiles
