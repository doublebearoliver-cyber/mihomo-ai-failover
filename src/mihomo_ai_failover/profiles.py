"""Safe Clash Verge profile-enhancement integration.

The runtime `clash-verge.yaml` file is generated and must never be edited
directly. This module updates the currently selected persistent Groups and
Rules enhancement files, preserving unrelated entries and creating a rollback
point before every change.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_AI_SUFFIXES, DEFAULT_GROUP_NAME

PROFILE_CONFIRMATION = "APPLY_PROFILE_INTEGRATION"
ROLLBACK_CONFIRMATION = "ROLLBACK_PROFILE_INTEGRATION"
ENHANCEMENT_TEMPLATE = {"prepend": [], "append": [], "delete": []}


class ProfileIntegrationError(RuntimeError):
    """Persistent Clash Verge profile integration failed safely."""


@dataclass(frozen=True)
class ProfilePlan:
    clash_root: Path
    profiles_path: Path
    current_profile_uid: str
    group_uid: str
    group_path: Path
    rules_uid: str
    rules_path: Path
    changes: tuple[str, ...]
    already_configured: bool

    def public(self) -> dict[str, Any]:
        return {
            "clash_root": str(self.clash_root),
            "current_profile_present": bool(self.current_profile_uid),
            "group_enhancement_file": self.group_path.name,
            "rules_enhancement_file": self.rules_path.name,
            "changes": list(self.changes),
            "already_configured": self.already_configured,
        }


@dataclass
class _PreparedPlan:
    plan: ProfilePlan
    profiles: dict[str, Any]
    groups: dict[str, Any]
    rules: dict[str, Any]
    group_created: bool
    rules_created: bool


def _load_yaml_mapping(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is None:
            raise ProfileIntegrationError(f"missing_file:{path.name}")
        return {
            key: list(value) if isinstance(value, list) else value for key, value in default.items()
        }
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileIntegrationError(f"invalid_yaml:{path.name}") from exc
    if not isinstance(data, dict):
        raise ProfileIntegrationError(f"mapping_required:{path.name}")
    return data


def _enhancement_mapping(path: Path) -> dict[str, Any]:
    data = _load_yaml_mapping(path, ENHANCEMENT_TEMPLATE)
    for key in ("prepend", "append", "delete"):
        value = data.setdefault(key, [])
        if not isinstance(value, list):
            raise ProfileIntegrationError(f"enhancement_list_required:{path.name}:{key}")
    return data


def _find_item(profiles: dict[str, Any], uid: str) -> dict[str, Any] | None:
    items = profiles.get("items")
    if not isinstance(items, list):
        raise ProfileIntegrationError("profiles_items_required")
    for item in items:
        if isinstance(item, dict) and item.get("uid") == uid:
            return item
    return None


def _new_uid(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def _safe_child(root: Path, relative: str | Path, label: str) -> Path:
    """Resolve a manifest/profile child without allowing path or symlink escape."""
    root_resolved = root.expanduser().resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise ProfileIntegrationError(f"unsafe_path:{label}")
    candidate = (root_resolved / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ProfileIntegrationError(f"unsafe_path:{label}") from exc
    return candidate


def _ensure_enhancement_item(
    profiles: dict[str, Any],
    current_item: dict[str, Any],
    kind: str,
    prefix: str,
    display_name: str,
    profiles_dir: Path,
) -> tuple[str, Path, bool]:
    options = current_item.setdefault("option", {})
    if not isinstance(options, dict):
        raise ProfileIntegrationError("current_profile_option_invalid")
    configured_uid = options.get(kind)
    if isinstance(configured_uid, str) and configured_uid:
        existing = _find_item(profiles, configured_uid)
        if existing is not None:
            filename = existing.get("file")
            if not isinstance(filename, str) or not filename:
                raise ProfileIntegrationError(f"{kind}_file_missing")
            return (
                configured_uid,
                _safe_child(profiles_dir, filename, f"{kind}_file"),
                False,
            )

    uid = _new_uid(prefix)
    filename = f"{uid}.yaml"
    item = {
        "uid": uid,
        "type": kind,
        "name": display_name,
        "file": filename,
        "updated": int(time.time()),
    }
    items = profiles.setdefault("items", [])
    if not isinstance(items, list):
        raise ProfileIntegrationError("profiles_items_required")
    items.append(item)
    options[kind] = uid
    return uid, _safe_child(profiles_dir, filename, f"{kind}_file"), True


def _group_entries(groups: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in ("prepend", "append"):
        for item in groups.get(key, []):
            if isinstance(item, dict):
                result.append(item)
    return result


def _ensure_group(groups: dict[str, Any], group_name: str) -> bool:
    matches = [item for item in _group_entries(groups) if item.get("name") == group_name]
    if len(matches) > 1:
        raise ProfileIntegrationError("duplicate_ai_group")
    if matches:
        existing = matches[0]
        group_type = str(existing.get("type") or "").lower()
        has_candidates = bool(existing.get("include-all-proxies")) or bool(existing.get("proxies"))
        if group_type != "select" or not has_candidates:
            raise ProfileIntegrationError("existing_ai_group_incompatible")
        return False
    groups["append"].append(
        {
            "name": group_name,
            "type": "select",
            "include-all-proxies": True,
        }
    )
    return True


def _parse_rule(rule: Any) -> tuple[str, str, str] | None:
    if not isinstance(rule, str):
        return None
    fields = [part.strip() for part in rule.split(",")]
    if len(fields) < 3:
        return None
    return fields[0].upper(), fields[1].lower().rstrip("."), fields[2]


def _ensure_rules(
    rules: dict[str, Any],
    group_name: str,
    suffixes: list[str],
    exact_domains: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    suffix_additions: list[str] = []
    domain_additions: list[str] = []
    existing_rules = list(rules.get("prepend", [])) + list(rules.get("append", []))
    parsed = [_parse_rule(rule) for rule in existing_rules]

    def ensure(
        rule_type: str,
        value: str,
        additions: list[str],
        *,
        before_suffixes: bool = False,
    ) -> None:
        normalized = value.lower().rstrip(".")
        matches = [
            value
            for value in parsed
            if value is not None and value[0] == rule_type and value[1] == normalized
        ]
        if any(value[2] != group_name for value in matches):
            raise ProfileIntegrationError(f"conflicting_domain_rule:{normalized}")
        if not matches:
            rule = f"{rule_type},{normalized},{group_name}"
            if before_suffixes:
                rules["prepend"].insert(0, rule)
            else:
                rules["prepend"].append(rule)
            additions.append(normalized)
            parsed.append((rule_type, normalized, group_name))

    for domain in exact_domains or []:
        ensure("DOMAIN", domain, domain_additions, before_suffixes=True)
    for suffix in suffixes:
        ensure("DOMAIN-SUFFIX", suffix, suffix_additions)
    return suffix_additions, domain_additions


def prepare_profile_integration(
    clash_root: Path | str,
    *,
    group_name: str = DEFAULT_GROUP_NAME,
    suffixes: list[str] | None = None,
    exact_domains: list[str] | None = None,
    provider_profiles: list[dict[str, Any]] | None = None,
) -> _PreparedPlan:
    if provider_profiles == []:
        raise ProfileIntegrationError("no_enabled_providers")
    root = Path(clash_root).expanduser().resolve()
    profiles_path = _safe_child(root, "profiles.yaml", "profiles_file")
    profiles_dir = _safe_child(root, "profiles", "profiles_directory")
    profiles = _load_yaml_mapping(profiles_path)
    current_uid = profiles.get("current")
    if not isinstance(current_uid, str) or not current_uid:
        raise ProfileIntegrationError("current_profile_missing")
    current_item = _find_item(profiles, current_uid)
    if current_item is None:
        raise ProfileIntegrationError("current_profile_item_missing")

    group_uid, group_path, group_created = _ensure_enhancement_item(
        profiles,
        current_item,
        "groups",
        "g",
        "Mihomo AI Failover Groups",
        profiles_dir,
    )
    rules_uid, rules_path, rules_created = _ensure_enhancement_item(
        profiles,
        current_item,
        "rules",
        "r",
        "Mihomo AI Failover Rules",
        profiles_dir,
    )
    groups = _enhancement_mapping(group_path)
    rules = _enhancement_mapping(rules_path)
    legacy_mode = provider_profiles is None
    routing = (
        [
            {
                "id": "openai",
                "group_name": group_name,
                "domain_suffixes": list(DEFAULT_AI_SUFFIXES if suffixes is None else suffixes),
                "exact_domains": list(exact_domains or []),
            }
        ]
        if provider_profiles is None
        else provider_profiles
    )
    changes: list[str] = []
    if group_created:
        changes.append("create_groups_enhancement")
    if rules_created:
        changes.append("create_rules_enhancement")
    for provider in routing:
        provider_id = str(provider.get("id") or "provider")
        provider_group = str(provider.get("group_name") or "")
        if not provider_group:
            raise ProfileIntegrationError(f"provider_group_missing:{provider_id}")
        group_added = _ensure_group(groups, provider_group)
        suffix_additions, domain_additions = _ensure_rules(
            rules,
            provider_group,
            list(provider.get("domain_suffixes", [])),
            list(provider.get("exact_domains", [])),
        )
        if group_added:
            changes.append(
                "add_ai_select_group" if legacy_mode else f"add_provider_select_group:{provider_id}"
            )
        changes.extend(
            (
                f"add_domain_suffix:{suffix}"
                if legacy_mode
                else f"add_domain_suffix:{provider_id}:{suffix}"
            )
            for suffix in suffix_additions
        )
        changes.extend(
            (f"add_domain:{domain}" if legacy_mode else f"add_domain:{provider_id}:{domain}")
            for domain in domain_additions
        )
    plan = ProfilePlan(
        clash_root=root,
        profiles_path=profiles_path,
        current_profile_uid=current_uid,
        group_uid=group_uid,
        group_path=group_path,
        rules_uid=rules_uid,
        rules_path=rules_path,
        changes=tuple(changes),
        already_configured=not changes,
    )
    return _PreparedPlan(
        plan=plan,
        profiles=profiles,
        groups=groups,
        rules=rules,
        group_created=group_created,
        rules_created=rules_created,
    )


def preview_profile_integration(
    clash_root: Path | str,
    *,
    group_name: str = DEFAULT_GROUP_NAME,
    suffixes: list[str] | None = None,
    exact_domains: list[str] | None = None,
    provider_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return prepare_profile_integration(
        clash_root,
        group_name=group_name,
        suffixes=suffixes,
        exact_domains=exact_domains,
        provider_profiles=provider_profiles,
    ).plan.public()


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _backup_files(
    prepared: _PreparedPlan,
    runtime_path: Path,
) -> tuple[Path, dict[str, Any]]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup = runtime_path / "backups" / f"profile-{timestamp}-{secrets.token_hex(3)}"
    originals = backup / "originals"
    originals.mkdir(parents=True, mode=0o700)
    targets = [
        prepared.plan.profiles_path,
        prepared.plan.group_path,
        prepared.plan.rules_path,
    ]
    original_files: list[str] = []
    created_files: list[str] = []
    hashes: dict[str, str | None] = {}
    for target in targets:
        relative = target.relative_to(prepared.plan.clash_root)
        relative_text = str(relative)
        hashes[relative_text] = _sha256(target)
        if target.exists():
            destination = originals / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(target, destination)
            original_files.append(relative_text)
        else:
            created_files.append(relative_text)
    manifest = {
        "format": 1,
        "created_at": int(time.time()),
        "clash_root": str(prepared.plan.clash_root),
        "original_files": original_files,
        "created_files": created_files,
        "before_sha256": hashes,
        "changes": list(prepared.plan.changes),
    }
    _write_json(backup / "manifest.json", manifest)
    return backup, manifest


def apply_profile_integration(
    clash_root: Path | str,
    runtime_path: Path | str,
    *,
    confirmation: str,
    group_name: str = DEFAULT_GROUP_NAME,
    suffixes: list[str] | None = None,
    exact_domains: list[str] | None = None,
    provider_profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if confirmation != PROFILE_CONFIRMATION:
        raise ProfileIntegrationError("explicit_confirmation_required")
    prepared = prepare_profile_integration(
        clash_root,
        group_name=group_name,
        suffixes=suffixes,
        exact_domains=exact_domains,
        provider_profiles=provider_profiles,
    )
    if prepared.plan.already_configured:
        return {
            **prepared.plan.public(),
            "changed": False,
            "backup": None,
            "restart_required": False,
        }
    backup, manifest = _backup_files(prepared, Path(runtime_path))
    before = manifest["before_sha256"]
    targets = {
        prepared.plan.profiles_path: prepared.profiles,
        prepared.plan.group_path: prepared.groups,
        prepared.plan.rules_path: prepared.rules,
    }
    try:
        for target in targets:
            relative = str(target.relative_to(prepared.plan.clash_root))
            if _sha256(target) != before.get(relative):
                raise ProfileIntegrationError(f"file_changed_during_apply:{relative}")
        # Enhancement files first, profiles.yaml last.
        _atomic_write_yaml(prepared.plan.group_path, prepared.groups)
        _atomic_write_yaml(prepared.plan.rules_path, prepared.rules)
        _atomic_write_yaml(prepared.plan.profiles_path, prepared.profiles)
    except Exception:
        _restore_backup(backup, prepared.plan.clash_root)
        raise
    return {
        **prepared.plan.public(),
        "changed": True,
        "backup": str(backup),
        "restart_required": True,
    }


def _restore_backup(backup: Path, clash_root: Path) -> None:
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    originals = backup / "originals"
    for relative_text in manifest.get("original_files", []):
        if not isinstance(relative_text, str):
            raise ProfileIntegrationError("backup_manifest_invalid")
        source = _safe_child(originals, relative_text, "backup_source")
        destination = _safe_child(clash_root, relative_text, "restore_destination")
        if not source.is_file():
            raise ProfileIntegrationError(f"backup_source_missing:{relative_text}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, destination)
    for relative_text in manifest.get("created_files", []):
        if not isinstance(relative_text, str):
            raise ProfileIntegrationError("backup_manifest_invalid")
        target = _safe_child(clash_root, relative_text, "rollback_created_file")
        if target.exists() and target.is_file():
            recovered = _safe_child(
                backup / "created-after-rollback",
                relative_text,
                "rollback_recovery_file",
            )
            recovered.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(target), str(recovered))


def latest_profile_backup(runtime_path: Path | str) -> Path | None:
    root = Path(runtime_path) / "backups"
    candidates = sorted(
        (path for path in root.glob("profile-*") if (path / "manifest.json").is_file()),
        reverse=True,
    )
    return candidates[0] if candidates else None


def rollback_profile_integration(
    runtime_path: Path | str,
    *,
    confirmation: str,
    backup_path: Path | str | None = None,
    expected_clash_root: Path | str | None = None,
) -> dict[str, Any]:
    if confirmation != ROLLBACK_CONFIRMATION:
        raise ProfileIntegrationError("explicit_confirmation_required")
    backup = latest_profile_backup(runtime_path) if backup_path is None else Path(backup_path)
    if backup is None:
        raise ProfileIntegrationError("backup_not_found")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    clash_root = Path(str(manifest.get("clash_root") or ""))
    if not clash_root.is_absolute():
        raise ProfileIntegrationError("backup_clash_root_invalid")
    clash_root = clash_root.resolve()
    if (
        expected_clash_root is not None
        and clash_root != Path(expected_clash_root).expanduser().resolve()
    ):
        raise ProfileIntegrationError("backup_clash_root_mismatch")
    _restore_backup(backup, clash_root)
    marker = backup / "rolled-back.json"
    _write_json(marker, {"rolled_back_at": int(time.time())})
    return {
        "rolled_back": True,
        "backup": str(backup),
        "restart_required": True,
    }
