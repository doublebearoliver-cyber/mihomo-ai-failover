from __future__ import annotations

import json
from pathlib import Path

import yaml

from mihomo_ai_failover import __version__

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "mihomo-ai-failover"


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
    skill_path = PLUGIN / "skills" / "openai-network-failover" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    _, frontmatter, _ = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "openai-network-failover"
    assert "Diagnose" in metadata["description"]

    codex_market = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude_market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    expected = "./plugins/mihomo-ai-failover"
    assert codex_market["plugins"][0]["source"]["path"] == expected
    assert claude_market["plugins"][0]["source"] == expected
    assert (ROOT / expected).is_dir()
