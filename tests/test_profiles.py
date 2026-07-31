from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mihomo_ai_failover.profiles import (
    PROFILE_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    ProfileIntegrationError,
    apply_profile_integration,
    preview_profile_integration,
    rollback_profile_integration,
)


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _fixture_with_existing_enhancements(root: Path) -> tuple[str, str, str]:
    profiles = {
        "current": "subscription",
        "items": [
            {
                "uid": "groups-existing",
                "type": "groups",
                "name": "Existing groups",
                "file": "groups-existing.yaml",
                "updated": 1,
            },
            {
                "uid": "rules-existing",
                "type": "rules",
                "name": "Existing rules",
                "file": "rules-existing.yaml",
                "updated": 1,
            },
            {
                "uid": "subscription",
                "type": "remote",
                "name": "Example subscription",
                "file": "subscription.yaml",
                "updated": 1,
                "option": {
                    "groups": "groups-existing",
                    "rules": "rules-existing",
                },
            },
        ],
    }
    groups = {
        "prepend": [],
        "append": [
            {
                "name": "Existing group",
                "type": "select",
                "proxies": ["Example node"],
            }
        ],
        "delete": [],
    }
    rules = {
        "prepend": ["DOMAIN-SUFFIX,example.com,Existing group"],
        "append": [],
        "delete": [],
    }
    _write_yaml(root / "profiles.yaml", profiles)
    _write_yaml(root / "profiles" / "groups-existing.yaml", groups)
    _write_yaml(root / "profiles" / "rules-existing.yaml", rules)
    return (
        (root / "profiles.yaml").read_text(encoding="utf-8"),
        (root / "profiles" / "groups-existing.yaml").read_text(encoding="utf-8"),
        (root / "profiles" / "rules-existing.yaml").read_text(encoding="utf-8"),
    )


def test_preview_and_apply_preserve_existing_enhancements(tmp_path: Path) -> None:
    clash_root = tmp_path / "clash"
    runtime = tmp_path / "runtime"
    originals = _fixture_with_existing_enhancements(clash_root)

    preview = preview_profile_integration(clash_root)
    assert not preview["already_configured"]
    assert "add_ai_select_group" in preview["changes"]
    assert len([item for item in preview["changes"] if item.startswith("add_domain_suffix:")]) == 5

    with pytest.raises(ProfileIntegrationError, match="confirmation"):
        apply_profile_integration(
            clash_root,
            runtime,
            confirmation="yes",
        )

    result = apply_profile_integration(
        clash_root,
        runtime,
        confirmation=PROFILE_CONFIRMATION,
    )
    assert result["changed"]
    assert result["restart_required"]
    groups = yaml.safe_load(
        (clash_root / "profiles" / "groups-existing.yaml").read_text(encoding="utf-8")
    )
    rules = yaml.safe_load(
        (clash_root / "profiles" / "rules-existing.yaml").read_text(encoding="utf-8")
    )
    assert any(item.get("name") == "Existing group" for item in groups["append"])
    assert any(item.get("name") == "🤖 AI稳定出口" for item in groups["append"])
    assert rules["prepend"][0] == "DOMAIN-SUFFIX,example.com,Existing group"
    assert sum("🤖 AI稳定出口" in rule for rule in rules["prepend"]) == 5

    second_preview = preview_profile_integration(clash_root)
    assert second_preview["already_configured"]

    rolled_back = rollback_profile_integration(
        runtime,
        confirmation=ROLLBACK_CONFIRMATION,
    )
    assert rolled_back["rolled_back"]
    assert (clash_root / "profiles.yaml").read_text(encoding="utf-8") == originals[0]
    assert (clash_root / "profiles" / "groups-existing.yaml").read_text(
        encoding="utf-8"
    ) == originals[1]
    assert (clash_root / "profiles" / "rules-existing.yaml").read_text(
        encoding="utf-8"
    ) == originals[2]


def test_apply_creates_missing_enhancements_and_rollback_recovers(tmp_path: Path) -> None:
    clash_root = tmp_path / "clash"
    runtime = tmp_path / "runtime"
    _write_yaml(
        clash_root / "profiles.yaml",
        {
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
        },
    )
    original_profiles = (clash_root / "profiles.yaml").read_text(encoding="utf-8")

    result = apply_profile_integration(
        clash_root,
        runtime,
        confirmation=PROFILE_CONFIRMATION,
    )
    assert result["changed"]
    assert (clash_root / "profiles" / result["group_enhancement_file"]).exists()
    assert (clash_root / "profiles" / result["rules_enhancement_file"]).exists()

    rollback_profile_integration(runtime, confirmation=ROLLBACK_CONFIRMATION)
    assert (clash_root / "profiles.yaml").read_text(encoding="utf-8") == original_profiles
    assert not (clash_root / "profiles" / result["group_enhancement_file"]).exists()
    assert not (clash_root / "profiles" / result["rules_enhancement_file"]).exists()


def test_conflicting_openai_rule_stops_without_writes(tmp_path: Path) -> None:
    clash_root = tmp_path / "clash"
    originals = _fixture_with_existing_enhancements(clash_root)
    rules_path = clash_root / "profiles" / "rules-existing.yaml"
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules["prepend"].append("DOMAIN-SUFFIX,openai.com,Other group")
    _write_yaml(rules_path, rules)
    before = rules_path.read_text(encoding="utf-8")

    with pytest.raises(ProfileIntegrationError, match="conflicting_domain_rule"):
        preview_profile_integration(clash_root)

    assert rules_path.read_text(encoding="utf-8") == before
    assert (clash_root / "profiles.yaml").read_text(encoding="utf-8") == originals[0]


def test_existing_enhancement_path_cannot_escape_profiles_directory(
    tmp_path: Path,
) -> None:
    clash_root = tmp_path / "clash"
    _fixture_with_existing_enhancements(clash_root)
    profiles_path = clash_root / "profiles.yaml"
    profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8"))
    profiles["items"][0]["file"] = "../../outside.yaml"
    _write_yaml(profiles_path, profiles)

    with pytest.raises(ProfileIntegrationError, match="unsafe_path:groups_file"):
        preview_profile_integration(clash_root)


def test_rollback_rejects_mismatched_clash_root(tmp_path: Path) -> None:
    clash_root = tmp_path / "clash"
    runtime = tmp_path / "runtime"
    _fixture_with_existing_enhancements(clash_root)
    applied = apply_profile_integration(
        clash_root,
        runtime,
        confirmation=PROFILE_CONFIRMATION,
    )

    with pytest.raises(ProfileIntegrationError, match="backup_clash_root_mismatch"):
        rollback_profile_integration(
            runtime,
            confirmation=ROLLBACK_CONFIRMATION,
            backup_path=applied["backup"],
            expected_clash_root=tmp_path / "different-clash-root",
        )
