from __future__ import annotations

import json
from pathlib import Path

import yaml

from mihomo_ai_failover import __version__
from mihomo_ai_failover.discovery import PROVIDER_OVERLAY_CONFIRMATION
from mihomo_ai_failover.installer import INSTALL_CONFIRMATION
from mihomo_ai_failover.mcp_server import (
    CONFIG_WRITE_CONFIRMATION,
    SERVICE_START_CONFIRMATION,
    SERVICE_STOP_CONFIRMATION,
    WEB_FEEDBACK_CONFIRMATION,
)
from mihomo_ai_failover.profiles import ROLLBACK_CONFIRMATION
from mihomo_ai_failover.service import SERVICE_UNINSTALL_CONFIRMATION

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "mihomo-ai-failover"
SKILL = PLUGIN / "skills" / "mihomo-ai-failover" / "SKILL.md"

READ_ONLY_TOOLS = {
    "diagnose_environment",
    "list_provider_profiles",
    "check_provider_paths",
    "discover_provider_domains",
    "preview_provider_overlay",
    "get_status",
    "run_health_check",
    "list_pools",
    "get_recent_events",
    "preview_install",
    "simulate_failover",
    "get_service_status",
}
MUTATION_TOOLS = {
    "initialize_config": CONFIG_WRITE_CONFIRMATION,
    "apply_provider_overlay": PROVIDER_OVERLAY_CONFIRMATION,
    "install_failover": INSTALL_CONFIRMATION,
    "start_monitor": SERVICE_START_CONFIRMATION,
    "stop_monitor": SERVICE_STOP_CONFIRMATION,
    "record_web_feedback": WEB_FEEDBACK_CONFIRMATION,
    "rollback_profile": ROLLBACK_CONFIRMATION,
    "uninstall_monitor": SERVICE_UNINSTALL_CONFIRMATION,
}


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_and_claude_manifests_share_identity_and_components() -> None:
    codex = _json(PLUGIN / ".codex-plugin" / "plugin.json")
    claude = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    for manifest in (codex, claude):
        assert manifest["name"] == "mihomo-ai-failover"
        assert manifest["version"] == __version__
        assert manifest["skills"] == "./skills/"
        assert manifest["mcpServers"] == "./.mcp.json"


def test_mcp_launcher_is_local_stdio_and_release_pinned() -> None:
    mcp = _json(PLUGIN / ".mcp.json")["mcpServers"]["mihomo-ai-failover"]
    assert mcp["command"] == "/bin/sh"
    assert mcp["cwd"] == "."
    assert "CLAUDE_PLUGIN_ROOT" in mcp["args"][1]
    launcher = (PLUGIN / "bin" / "mihomo-ai-failover-mcp").read_text(encoding="utf-8")
    assert f"@v{__version__}" in launcher
    assert "mihomo-ai-failover-mcp" in launcher
    assert "http://" not in launcher


def test_skill_frontmatter_and_marketplaces_reference_real_plugin() -> None:
    text = SKILL.read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "mihomo-ai-failover"
    for search_term in ("ChatGPT", "Codex", "Clash Verge Rev", "Mihomo", "login"):
        assert search_term in metadata["description"]

    codex_market = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude_market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    expected = "./plugins/mihomo-ai-failover"
    assert codex_market["plugins"][0]["source"]["path"] == expected
    assert claude_market["plugins"][0]["source"] == expected
    assert (ROOT / expected).is_dir()


def test_agent_contract_is_discoverable_and_covers_every_mcp_tool() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "agent-integration.md").read_text(encoding="utf-8")
    plugin_readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

    canonical_path = "plugins/mihomo-ai-failover/skills/mihomo-ai-failover/SKILL.md"
    for readme in ("README.md", "README.zh-CN.md"):
        text = (ROOT / readme).read_text(encoding="utf-8")
        assert canonical_path in text
        assert "docs/agent-integration.md" in text
        assert "--skill mihomo-ai-failover --agent codex --global --yes" in text

    assert "skills/mihomo-ai-failover/SKILL.md" in plugin_readme
    for heading in (
        "## When to use this skill",
        "## Authority and trust",
        "## Non-negotiable boundaries",
        "## Classify evidence correctly",
        "## Stop and ask the user",
        "## Report results",
    ):
        assert heading in skill

    for tool in READ_ONLY_TOOLS | MUTATION_TOOLS.keys():
        marker = f"`{tool}`"
        assert marker in skill
        assert marker in integration

    for confirmation in MUTATION_TOOLS.values():
        assert f"`{confirmation}`" in integration


def test_agent_contract_preserves_failover_and_privacy_boundaries() -> None:
    skill = SKILL.read_text(encoding="utf-8")
    required_phrases = (
        "at least two distinct critical targets fail",
        "Default connection draining is `preserve`",
        "Never enable TUN",
        "Never modify Mihomo's generated runtime YAML",
        "Do not call an installation or service mutation during diagnosis",
        "Never include controller secrets",
    )
    for phrase in required_phrases:
        assert phrase in skill

    references = SKILL.parent / "references"
    assert (references / "provider-adaptation.md").is_file()
    assert (references / "public-profiles.md").is_file()


def test_deprecated_skill_name_is_not_published() -> None:
    deprecated = "openai-network-failover"
    checked_files = (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "agent-integration.md",
        PLUGIN / "README.md",
        SKILL,
    )
    for path in checked_files:
        assert deprecated not in path.read_text(encoding="utf-8")
