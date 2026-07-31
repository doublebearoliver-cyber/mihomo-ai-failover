from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _scanner():
    path = Path(__file__).parents[1] / "scripts" / "scan_sensitive.py"
    spec = importlib.util.spec_from_file_location("scan_sensitive", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scanner_rejects_home_path_and_token() -> None:
    scanner = _scanner()
    token = "ghp_" + ("A" * 36)
    private_home = "/" + "Users" + "/private-user/project"
    findings = scanner.scan_text(
        "fixture",
        "example.txt",
        f"{private_home}\n{token}\n",
    )
    kinds = {finding.kind for finding in findings}
    assert "absolute user home" in kinds
    assert "GitHub token" in kinds


def test_scanner_accepts_documentation_ips_in_tests() -> None:
    scanner = _scanner()
    findings = scanner.scan_text(
        "fixture",
        "tests/example.py",
        "192.0.2.1 198.51.100.2 203.0.113.3 127.0.0.1",
    )
    assert not findings


def test_scanner_rejects_real_public_ip_in_tests() -> None:
    scanner = _scanner()
    value = ".".join(["8", "8", "8", "8"])
    findings = scanner.scan_text("fixture", "tests/example.py", value)
    assert any(item.kind == "test uses non-documentation public IP" for item in findings)
