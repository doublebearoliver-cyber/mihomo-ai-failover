"""Public command-line interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import engine
from .config import (
    ConfigError,
    default_config,
    default_config_path,
    load_config,
    write_config,
)
from .diagnostics import diagnose_environment
from .installer import install_local
from .profiles import (
    ProfileIntegrationError,
    apply_profile_integration,
    preview_profile_integration,
    rollback_profile_integration,
)
from .service import (
    ServiceError,
    install_service,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)

CORE_COMMANDS = {"daemon", "run-once", "status", "check", "inventory"}


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=default_config_path())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mihomo-ai-failover",
        description="OpenAI-aware failover for Mihomo on macOS",
    )
    subparsers = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("status", "Show controller, current route, and pool status"),
        ("check", "Run a read-only direct and OpenAI health check"),
        ("run-once", "Run one health iteration; may switch after confirmed failures"),
        ("inventory", "Build or refresh the three-layer node inventory"),
        ("daemon", "Run the long-lived failover monitor"),
    ):
        item = subparsers.add_parser(name, help=help_text)
        _add_config_argument(item)

    init = subparsers.add_parser("init", help="Write an auto-discovered local config")
    _add_config_argument(init)
    init.add_argument("--force", action="store_true")

    diagnose = subparsers.add_parser("diagnose", help="Inspect the local environment")
    _add_config_argument(diagnose)

    web_feedback = subparsers.add_parser(
        "web-feedback",
        help="Record time-limited real-browser evidence for one observed exit",
    )
    _add_config_argument(web_feedback)
    web_feedback.add_argument("--node", required=True)
    web_feedback.add_argument(
        "--status",
        required=True,
        choices=(engine.WEB_FEEDBACK_CONFIRMED, engine.WEB_FEEDBACK_REJECTED),
    )
    web_feedback.add_argument("--reason", default="manual_browser_validation")
    web_feedback.add_argument("--ttl-seconds", type=int)
    web_feedback.add_argument("--confirm", required=True)

    profile_preview = subparsers.add_parser(
        "profile-preview",
        help="Preview persistent Clash Verge group/rule integration",
    )
    _add_config_argument(profile_preview)

    profile_install = subparsers.add_parser(
        "profile-install",
        help="Back up and apply persistent Clash Verge integration",
    )
    _add_config_argument(profile_install)
    profile_install.add_argument("--confirm", required=True)

    profile_rollback = subparsers.add_parser(
        "profile-rollback",
        help="Restore the latest persistent-profile backup",
    )
    _add_config_argument(profile_rollback)
    profile_rollback.add_argument("--backup", type=Path)
    profile_rollback.add_argument("--confirm", required=True)

    service_show = subparsers.add_parser("service-status", help="Show LaunchAgent status")
    _add_config_argument(service_show)

    service_install_parser = subparsers.add_parser(
        "service-install",
        help="Install the per-user LaunchAgent",
    )
    _add_config_argument(service_install_parser)
    service_install_parser.add_argument("--confirm", required=True)
    service_install_parser.add_argument("--start", action="store_true")

    for name, help_text in (
        ("service-start", "Start or restart the LaunchAgent"),
        ("service-stop", "Stop the LaunchAgent"),
    ):
        item = subparsers.add_parser(name, help=help_text)
        _add_config_argument(item)

    service_uninstall_parser = subparsers.add_parser(
        "service-uninstall",
        help="Stop and move the LaunchAgent plist to Trash",
    )
    _add_config_argument(service_uninstall_parser)
    service_uninstall_parser.add_argument("--confirm", required=True)

    install = subparsers.add_parser(
        "install",
        help="Initialize config, integrate the AI group/rules, and install LaunchAgent",
    )
    _add_config_argument(install)
    install.add_argument("--confirm", required=True)
    install.add_argument("--start", action="store_true")

    return parser


def _print(value: dict[str, Any]) -> None:
    engine.print_output(value)


def _runtime_log_dir(config: dict[str, Any]) -> Path:
    return Path(str(config["log_path"])).parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        return engine.main(["status", "--config", str(default_config_path())])
    command = args.command
    if command in CORE_COMMANDS:
        return engine.main([command, "--config", str(args.config)])
    if command == "web-feedback":
        forwarded = [
            command,
            "--config",
            str(args.config),
            "--node",
            args.node,
            "--web-status",
            args.status,
            "--reason",
            args.reason,
            "--confirm",
            args.confirm,
        ]
        if args.ttl_seconds is not None:
            forwarded.extend(("--ttl-seconds", str(args.ttl_seconds)))
        return engine.main(forwarded)

    try:
        if command == "init":
            config = default_config()
            target = write_config(args.config, config, overwrite=args.force)
            output = {"created": True, "config": str(target)}
        elif command == "diagnose":
            output = diagnose_environment(load_config(args.config))
        elif command == "profile-preview":
            config = load_config(args.config)
            output = preview_profile_integration(
                config["clash_data_root"],
                group_name=config["group_name"],
                suffixes=list(config["ai_domain_suffixes"]),
            )
        elif command == "profile-install":
            config = load_config(args.config)
            output = apply_profile_integration(
                config["clash_data_root"],
                config["runtime_path"],
                confirmation=args.confirm,
                group_name=config["group_name"],
                suffixes=list(config["ai_domain_suffixes"]),
            )
        elif command == "profile-rollback":
            config = load_config(args.config)
            output = rollback_profile_integration(
                config["runtime_path"],
                confirmation=args.confirm,
                backup_path=args.backup,
                expected_clash_root=config["clash_data_root"],
            )
        elif command == "service-status":
            output = service_status()
        elif command == "service-install":
            config = load_config(args.config)
            output = install_service(
                args.config,
                _runtime_log_dir(config),
                confirmation=args.confirm,
                start=args.start,
            )
        elif command == "service-start":
            output = start_service()
        elif command == "service-stop":
            output = stop_service()
        elif command == "service-uninstall":
            output = uninstall_service(confirmation=args.confirm)
        elif command == "install":
            output = install_local(
                args.config,
                confirmation=args.confirm,
                start=args.start,
            )
        else:
            parser.error(f"unknown command: {command}")
            return 2
    except (
        ConfigError,
        FileExistsError,
        OSError,
        ProfileIntegrationError,
        ServiceError,
    ) as exc:
        _print(
            {
                "ok": False,
                "command": command,
                "error": str(exc) or type(exc).__name__,
            }
        )
        return 2
    _print({"ok": True, "command": command, "result": output})
    return 0
