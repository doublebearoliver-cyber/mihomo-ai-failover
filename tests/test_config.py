from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mihomo_ai_failover.config import (
    ConfigError,
    default_config,
    load_config,
    validate_config,
    write_config,
)


def test_default_config_discovers_runtime_port_and_socket(tmp_path: Path) -> None:
    clash_root = (
        tmp_path / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev"
    )
    clash_root.mkdir(parents=True)
    (clash_root / "clash-verge.yaml").write_text(
        "mixed-port: 8123\n"
        "external-controller-unix: /tmp/example/verge.sock\n"
        "secret: do-not-copy-me\n"
        "proxies:\n"
        "  - name: private\n"
        "    password: private\n",
        encoding="utf-8",
    )

    config = default_config(home=tmp_path, clash_root=clash_root)

    assert config["mixed_proxy_url"] == "http://127.0.0.1:8123"
    assert config["clash_socket_path"] == "/tmp/example/verge.sock"
    assert "secret" not in config
    assert "password" not in config
    assert config["web_feedback_confirmed_ttl_seconds"] == 604800
    assert config["web_feedback_rejected_ttl_seconds"] == 86400
    assert config["candidate_reverification_delay_seconds"] == 3


def test_config_round_trip_expands_home_without_secrets(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    config = default_config(home=tmp_path)
    config["runtime_path"] = "~/Library/Application Support/Test Failover"
    previous_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        write_config(target, config)
        loaded = load_config(target, home=tmp_path)
    finally:
        if previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous_home

    assert loaded["runtime_path"].startswith(str(tmp_path))
    assert target.stat().st_mode & 0o077 == 0


def test_config_rejects_non_loopback_proxy() -> None:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-test"))
    config["mixed_proxy_url"] = "http://192.0.2.20:7890"
    with pytest.raises(ConfigError, match="loopback"):
        validate_config(config)


def test_config_requires_two_failure_rounds() -> None:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-test"))
    config["failure_rounds_before_switch"] = 1
    with pytest.raises(ConfigError, match="at_least_2"):
        validate_config(config)


def test_config_requires_positive_web_feedback_ttl() -> None:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-test"))
    config["web_feedback_rejected_ttl_seconds"] = 0
    with pytest.raises(ConfigError, match="web_feedback_rejected_ttl_seconds"):
        validate_config(config)


def test_config_rejects_negative_hard_probe_retries() -> None:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-test"))
    config["hard_probe_retry_count"] = -1
    with pytest.raises(ConfigError, match="hard_probe_retry_count"):
        validate_config(config)


def test_config_rejects_negative_candidate_reverification_delay() -> None:
    config = default_config(home=Path("/tmp/mihomo-ai-failover-test"))
    config["candidate_reverification_delay_seconds"] = -1
    with pytest.raises(ConfigError, match="candidate_reverification_delay_seconds"):
        validate_config(config)


def test_old_config_is_upgraded_with_ws_probe(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "config_version": 1,
                "active_probes": [
                    {
                        "name": "openai_api",
                        "kind": "openai_api",
                        "url": "https://api.openai.com/v1/models",
                        "timeout_seconds": 6,
                        "connect_timeout_seconds": 4,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(target, home=tmp_path)
    names = {item["name"] for item in loaded["active_probes"]}
    assert loaded["config_version"] == 2
    assert "openai_api" in names
    assert "chatgpt_ws" in names
