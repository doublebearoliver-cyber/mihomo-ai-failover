from __future__ import annotations

from pathlib import Path

from mihomo_ai_failover import engine
from mihomo_ai_failover.config import default_config, write_config


def _entry() -> dict:
    entry = engine.node_template("ss")
    entry.update(
        {
            "exit_ip": "192.0.2.10",
            "exit_country": "CH",
            "asn": "AS64500",
            "openai_status": "healthy",
            "deep_verified_at": 10_000,
        }
    )
    return entry


def test_web_feedback_cli_requires_confirmation_and_persists(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    config["runtime_path"] = str(runtime)
    config["log_path"] = str(tmp_path / "logs" / "monitor.jsonl")
    config_path = tmp_path / "config.yaml"
    write_config(config_path, config)

    runtime.mkdir()
    state = engine.default_state()
    state["nodes"] = {"瑞士出口": _entry()}
    engine.atomic_write_json(runtime / "state.json", state)

    missing_confirmation = engine.main(
        [
            "web-feedback",
            "--config",
            str(config_path),
            "--node",
            "瑞士出口",
            "--web-status",
            "confirmed",
        ]
    )
    assert missing_confirmation == 2
    unchanged = engine.load_state(runtime / "state.json")
    assert unchanged["nodes"]["瑞士出口"]["web_feedback"] is None

    recorded = engine.main(
        [
            "web-feedback",
            "--config",
            str(config_path),
            "--node",
            "瑞士出口",
            "--web-status",
            "confirmed",
            "--reason",
            "browser_login_success",
            "--confirm",
            engine.WEB_FEEDBACK_CONFIRMATION,
        ]
    )
    assert recorded == 0
    persisted = engine.load_state(runtime / "state.json")
    assert (
        engine.web_feedback_status(
            persisted["nodes"]["瑞士出口"],
            engine.now_ts(),
        )
        == engine.WEB_FEEDBACK_CONFIRMED
    )
