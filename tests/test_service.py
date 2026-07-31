from __future__ import annotations

import plistlib
from pathlib import Path
from unittest import mock

import pytest

from mihomo_ai_failover import service


def test_launch_agent_uses_supplied_executable_and_no_proxy(tmp_path: Path) -> None:
    document = service.build_launch_agent(
        tmp_path / "config.yaml",
        tmp_path / "logs",
        executable=tmp_path / "bin" / "mihomo-ai-failover",
    )
    assert document["ProgramArguments"] == [
        str(tmp_path / "bin" / "mihomo-ai-failover"),
        "daemon",
        "--config",
        str(tmp_path / "config.yaml"),
    ]
    assert document["EnvironmentVariables"]["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert "HTTP_PROXY" not in document["EnvironmentVariables"]


def test_install_service_writes_user_launch_agent_in_temp_home(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "bin" / "mihomo-ai-failover"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    with mock.patch.object(service.sys, "platform", "darwin"):
        result = service.install_service(
            tmp_path / "config.yaml",
            tmp_path / "logs",
            confirmation=service.SERVICE_INSTALL_CONFIRMATION,
            executable=executable,
            home=tmp_path,
        )

    plist_path = Path(result["plist"])
    assert plist_path.exists()
    document = plistlib.loads(plist_path.read_bytes())
    assert document["Label"] == service.SERVICE_LABEL
    assert document["ProgramArguments"][0] == str(executable)
    assert plist_path.stat().st_mode & 0o077 == 0


def test_uninstall_moves_plist_to_temp_home_trash(tmp_path: Path) -> None:
    plist_path = service.launch_agent_path(tmp_path)
    plist_path.parent.mkdir(parents=True)
    plist_path.write_text("temporary plist", encoding="utf-8")
    completed = mock.Mock(returncode=1, stdout="", stderr="")

    with (
        mock.patch.object(service.sys, "platform", "darwin"),
        mock.patch.object(service.subprocess, "run", return_value=completed),
    ):
        result = service.uninstall_service(
            confirmation=service.SERVICE_UNINSTALL_CONFIRMATION,
            home=tmp_path,
        )

    assert result["uninstalled"] is True
    assert not plist_path.exists()
    assert Path(result["recoverable_at"]).is_file()
    assert Path(result["recoverable_at"]).parent == tmp_path / ".Trash"


def test_uninstall_requires_exact_confirmation(tmp_path: Path) -> None:
    with pytest.raises(service.ServiceError, match="explicit_confirmation_required"):
        service.uninstall_service(confirmation="yes", home=tmp_path)
