#!/usr/bin/env python3
"""Fail a release when repository content resembles private network material."""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT_BYTES = 2_000_000
FORBIDDEN_NAMES = {
    ".env",
    "config.local.yaml",
    "state.json",
    "monitor.jsonl",
}
FORBIDDEN_SUFFIXES = {".pem", ".p12", ".pfx"}
GENERAL_PATTERNS = {
    "absolute user home": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-bearing URL": re.compile(
        r"https?://[^\s\"'<>]+[?&](?:token|subscription|key|auth)="
        r"[A-Za-z0-9%._~-]{12,}",
        re.IGNORECASE,
    ),
}
CONFIG_CREDENTIAL = re.compile(
    r"(?mi)^\s*(?:secret|password|uuid|client-secret|private-key)\s*:"
    r"\s*['\"]?(?!null\b|false\b|true\b|change-me\b|example\b|\{\{)(\S+)"
)
IPV4 = re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])")
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Finding:
    source: str
    path: str
    line: int
    kind: str

    def render(self) -> str:
        return f"{self.source}:{self.path}:{self.line}: {self.kind}"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _worktree_paths(root: Path) -> list[Path]:
    result = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if result.returncode != 0:
        return sorted(path for path in root.rglob("*") if path.is_file())
    return sorted(
        root / value
        for value in result.stdout.split("\0")
        if value and not value.startswith(".git/")
    )


def _is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= MAX_TEXT_BYTES


def _line_number(text: str, start: int) -> int:
    return text.count("\n", 0, start) + 1


def _is_documentation_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    return any(
        address in network
        for network in (
            ipaddress.ip_network("192.0.2.0/24"),
            ipaddress.ip_network("198.51.100.0/24"),
            ipaddress.ip_network("203.0.113.0/24"),
        )
    )


def scan_text(source: str, path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in GENERAL_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append(Finding(source, path, _line_number(text, match.start()), kind))
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml", ".json", ".toml"}:
        for match in CONFIG_CREDENTIAL.finditer(text):
            findings.append(
                Finding(
                    source,
                    path,
                    _line_number(text, match.start()),
                    "embedded credential field",
                )
            )
    if path.startswith("tests/"):
        for match in IPV4.finditer(text):
            if not _is_documentation_ip(match.group(0)):
                findings.append(
                    Finding(
                        source,
                        path,
                        _line_number(text, match.start()),
                        "test uses non-documentation public IP",
                    )
                )
    return findings


def scan_worktree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _worktree_paths(root):
        relative = path.relative_to(root).as_posix()
        if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(Finding("worktree", relative, 1, "forbidden private file"))
            continue
        try:
            if not _is_text_candidate(path):
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text("worktree", relative, text))
    return findings


def scan_history(root: Path) -> list[Finding]:
    objects = _git(root, "rev-list", "--objects", "--all")
    if objects.returncode != 0 or not objects.stdout.strip():
        return []
    findings: list[Finding] = []
    seen: set[str] = set()
    for line in objects.stdout.splitlines():
        object_id, _, path = line.partition(" ")
        if not path or object_id in seen:
            continue
        seen.add(object_id)
        suffix = Path(path).suffix.lower()
        if Path(path).name in FORBIDDEN_NAMES or suffix in FORBIDDEN_SUFFIXES:
            findings.append(Finding("history", path, 1, "forbidden private file"))
            continue
        if suffix not in TEXT_SUFFIXES:
            continue
        size = _git(root, "cat-file", "-s", object_id)
        if size.returncode != 0 or not size.stdout.strip().isdigit():
            continue
        if int(size.stdout.strip()) > MAX_TEXT_BYTES:
            continue
        blob = _git(root, "cat-file", "blob", object_id)
        if blob.returncode != 0:
            continue
        findings.extend(scan_text("history", path, blob.stdout))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="scan only the index and working tree",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    findings = scan_worktree(root)
    if not args.no_history:
        findings.extend(scan_history(root))
    unique = sorted(set(findings), key=lambda item: item.render())
    if unique:
        print("Sensitive-data scan failed:")
        for finding in unique:
            print(f"- {finding.render()}")
        return 1
    print("Sensitive-data scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
