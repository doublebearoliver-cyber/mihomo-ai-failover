from __future__ import annotations

from pathlib import Path

import pytest

from mihomo_ai_failover.config import default_config
from mihomo_ai_failover.providers import (
    ProviderError,
    builtin_provider_templates,
    enabled_provider_ids,
    merge_provider_overlay,
    normalize_hostname,
    normalize_providers,
    resolve_provider_config,
)


def test_public_templates_are_conservative_and_only_openai_is_enabled() -> None:
    providers = normalize_providers(builtin_provider_templates())

    assert set(providers) == {"openai", "workbuddy-cn", "kimi", "minimax", "mavis"}
    assert [key for key, value in providers.items() if value["enabled"]] == ["openai"]
    assert providers["workbuddy-cn"]["domain_suffixes"] == ["workbuddy.cn"]
    assert providers["kimi"]["domain_suffixes"] == ["kimi.com"]
    assert providers["minimax"]["domain_suffixes"] == ["minimaxi.com"]
    assert providers["mavis"]["domain_suffixes"] == ["mavislabs.ai"]


def test_provider_overlay_enables_one_provider_without_affecting_others() -> None:
    providers = normalize_providers()
    merged = merge_provider_overlay(
        providers,
        {
            "providers": {
                "kimi": {
                    "enabled": True,
                    "exact_domains": ["api.kimi-service.example"],
                }
            }
        },
    )

    config = {"providers": merged}
    assert enabled_provider_ids(config) == ["openai", "kimi"]
    assert merged["kimi"]["exact_domains"] == ["api.kimi-service.example"]
    assert merged["openai"]["exact_domains"] == []


def test_non_default_provider_has_isolated_runtime_and_log_paths(tmp_path: Path) -> None:
    config = default_config(home=tmp_path, clash_root=tmp_path / "clash")
    selected = resolve_provider_config(config, "kimi")

    assert selected["provider_id"] == "kimi"
    assert selected["runtime_path"].endswith("providers/kimi")
    assert selected["log_path"].endswith("providers/kimi/monitor.jsonl")
    assert selected["group_name"] == "🤖 Kimi稳定出口"


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "2001:db8::1", "*.example.com", "example", "bad/path.example"],
)
def test_provider_domains_reject_ips_wildcards_and_non_hosts(value: str) -> None:
    with pytest.raises(ProviderError):
        normalize_hostname(value)


def test_provider_group_cannot_break_clash_rule_syntax() -> None:
    providers = builtin_provider_templates()
    providers["kimi"]["group_name"] = "bad,group"
    with pytest.raises(ProviderError, match="provider_group_name_required:kimi"):
        normalize_providers(providers)


def test_provider_probe_must_use_a_routed_non_shared_host() -> None:
    providers = builtin_provider_templates()
    providers["kimi"]["active_probes"] = [
        {
            "name": "shared_auth",
            "kind": "generic_web",
            "url": "https://accounts.google.com/",
            "expected_statuses": [200],
            "timeout_seconds": 6,
            "connect_timeout_seconds": 4,
        }
    ]
    providers["kimi"]["exact_domains"] = ["accounts.google.com"]
    providers["kimi"]["required_probe_names"] = ["shared_auth"]
    providers["kimi"]["browser_ambiguous_probe_names"] = []

    with pytest.raises(ProviderError, match="shared_infrastructure"):
        normalize_providers(providers)


def test_provider_probe_kind_is_whitelisted() -> None:
    providers = builtin_provider_templates()
    providers["mavis"]["active_probes"][0]["kind"] = "shell_command"
    with pytest.raises(ProviderError, match="invalid_provider_probe:mavis"):
        normalize_providers(providers)
