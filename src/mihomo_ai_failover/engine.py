"""Mihomo 的 OpenAI 专用高可用监控。

设计边界：
- 只切换“🤖 AI稳定出口”，不改变全局节点、系统代理或 TUN。
- 当前节点健康时绝不因延迟变化切换。
- 只有连续两轮可验证硬故障才切换。
- 节点凭据只从 Clash 运行时配置读入内存，不写入本项目状态或日志。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import yaml

from .config import (
    DEFAULT_AI_SUFFIXES,
    default_config_path,
)
from .config import (
    load_config as load_public_config,
)

DEFAULT_CONFIG = default_config_path()
SCHEMA_VERSION = 2
SCANNER_GROUP = "🔬 AI出口扫描"
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Compatible"}


class ControllerError(RuntimeError):
    """Mihomo 控制接口错误。"""


class ScannerError(RuntimeError):
    """隔离扫描器错误。"""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class ClashController:
    def __init__(self, socket_path: str, secret: str):
        self.socket_path = socket_path
        self.secret = secret

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        response: http.client.HTTPResponse | None = None
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise ControllerError(type(exc).__name__) from exc
        finally:
            conn.close()
        if response is None or response.status >= 400:
            status = response.status if response is not None else 0
            raise ControllerError(f"controller_http_{status}")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControllerError("controller_invalid_json") from exc

    def version(self) -> dict[str, Any]:
        return self.request("GET", "/version")

    def proxies(self) -> dict[str, Any]:
        return self.request("GET", "/proxies").get("proxies", {})

    def proxy(self, proxy_name: str) -> dict[str, Any]:
        result = self.request(
            "GET",
            f"/proxies/{quote(proxy_name, safe='')}",
            timeout=4.0,
        )
        return result if isinstance(result, dict) else {}

    def select(self, group_name: str, proxy_name: str) -> None:
        self.request(
            "PUT",
            f"/proxies/{quote(group_name, safe='')}",
            {"name": proxy_name},
        )

    def delay(
        self,
        proxy_name: str,
        url: str,
        timeout_ms: int,
        expected: str,
    ) -> int | None:
        query = urlencode(
            {
                "url": url,
                "timeout": str(timeout_ms),
                "expected": expected,
            }
        )
        path = f"/proxies/{quote(proxy_name, safe='')}/delay?{query}"
        try:
            result = self.request("GET", path, timeout=max(6.0, timeout_ms / 1000 + 3))
        except ControllerError:
            return None
        delay = result.get("delay")
        return delay if isinstance(delay, int) and delay >= 0 else None

    def close_old_ai_connections(
        self,
        old_node: str,
        suffixes: Sequence[str],
    ) -> int:
        """只关闭仍绑定旧节点的 OpenAI 连接。"""
        try:
            result = self.request("GET", "/connections")
        except ControllerError:
            return 0
        closed = 0
        for connection in result.get("connections", []):
            metadata = connection.get("metadata", {}) or {}
            host = str(metadata.get("host") or "").lower().rstrip(".")
            if not host_matches_suffixes(host, suffixes):
                continue
            chains = [str(item) for item in connection.get("chains", [])]
            if old_node not in chains:
                continue
            connection_id = connection.get("id")
            if not connection_id:
                continue
            try:
                self.request(
                    "DELETE",
                    f"/connections/{quote(str(connection_id), safe='')}",
                )
                closed += 1
            except ControllerError:
                continue
        return closed


def now_ts() -> int:
    return int(time.time())


def iso_time(timestamp: int | None = None) -> str:
    value = now_ts() if timestamp is None else int(timestamp)
    return dt.datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def clean_text(value: Any, limit: int = 180) -> str:
    return str(value).replace("\n", " ").replace("\r", " ")[:limit]


def load_config(path: Path) -> dict[str, Any]:
    return load_public_config(path)


def safe_yaml_load(text: str) -> Any:
    """优先使用 LibYAML 安全加载器，降低周期扫描 CPU 占用。"""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(text, Loader=loader)


def read_secret(clash_config_path: str) -> str:
    content = Path(clash_config_path).read_text(encoding="utf-8")
    match = re.search(r"^secret:\s*(.*?)\s*$", content, re.MULTILINE)
    if not match:
        raise ControllerError("secret_missing")
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not value:
        raise ControllerError("secret_empty")
    return value


def controller_from_config(config: dict[str, Any]) -> ClashController:
    secret = read_secret(config["clash_config_path"])
    # 具体调用会验证 socket、密钥和权限；守护循环无需每 10 秒请求 /version。
    return ClashController(config["clash_socket_path"], secret)


def ensure_runtime(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    runtime = Path(config["runtime_path"])
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    state_path = runtime / "state.json"
    lock_path = runtime / "monitor.lock"
    log_path = Path(config["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(log_path.parent, 0o700)
    return state_path, lock_path, log_path


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def log_event(log_path: Path, event: str, **fields: Any) -> None:
    record: dict[str, Any] = {
        "time": iso_time(),
        "event": clean_text(event, 60),
    }
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (int, float, bool)):
            record[key] = value
        elif isinstance(value, list):
            record[key] = [clean_text(item) for item in value[:30]]
        elif isinstance(value, dict):
            record[key] = {
                clean_text(item_key, 60): clean_text(item_value)
                for item_key, item_value in list(value.items())[:30]
            }
        else:
            record[key] = clean_text(value)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with suppress(OSError):
        os.chmod(log_path, 0o600)


def notify(title: str, message: str) -> None:
    def apple_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')[:240]

    script = f'display notification "{apple_escape(message)}" with title "{apple_escape(title)}"'
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


def host_matches_suffixes(host: str, suffixes: Sequence[str]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in suffixes)


def error_kind(stderr: str, exit_code: int) -> str:
    lower = stderr.lower()
    if "timed out" in lower or exit_code == 28:
        return "timeout"
    if "connection reset" in lower or "reset by peer" in lower:
        return "connection_reset"
    if "ssl" in lower or "tls" in lower or exit_code in {35, 51, 60}:
        return "tls_failure"
    if "refused" in lower:
        return "connection_refused"
    if "unreachable" in lower:
        return "network_unreachable"
    if "could not resolve" in lower or exit_code == 6:
        return "dns_failure"
    if exit_code != 0:
        return f"curl_exit_{exit_code}"
    return "none"


def parse_curl_output(stdout: bytes) -> tuple[int, int, dict[str, str], bytes]:
    marker = b"\n__AIWATCH__ "
    if marker not in stdout:
        return 0, 0, {}, b""
    payload, metrics = stdout.rsplit(marker, 1)
    fields = metrics.strip().split()
    try:
        status = int(fields[0])
        latency_ms = int(float(fields[1]) * 1000)
    except (IndexError, TypeError, ValueError):
        status, latency_ms = 0, 0

    header_pattern = re.compile(rb"(?m)^HTTP/[^\r\n]+\r?\n(?:[^\r\n]+\r?\n)*\r?\n")
    matches = list(header_pattern.finditer(payload))
    if not matches:
        return status, latency_ms, {}, payload
    final_header = matches[-1]
    header_bytes = final_header.group(0)
    headers: dict[str, str] = {}
    for raw_line in header_bytes.splitlines()[1:]:
        if b":" not in raw_line:
            continue
        key, value = raw_line.split(b":", 1)
        headers[key.decode(errors="ignore").lower()] = value.decode(errors="ignore").strip()
    return status, latency_ms, headers, payload[final_header.end() :]


def classify_probe(
    kind: str,
    status: int,
    headers: dict[str, str],
    body: bytes,
    exit_code: int,
    stderr: str,
) -> tuple[str, str]:
    """返回 verdict(healthy/soft/hard) 和脱敏原因。"""
    if exit_code != 0 or status == 0:
        return "hard", error_kind(stderr, exit_code)

    text = body[:65536].decode("utf-8", errors="ignore").lower()
    content_type = headers.get("content-type", "").lower()
    challenged = headers.get("cf-mitigated", "").lower() == "challenge"
    challenged = challenged or "cf-chl-" in text or "just a moment" in text
    unsupported = any(
        marker in text
        for marker in (
            "unsupported_country",
            "unsupported country",
            "region is not supported",
            "not available in your country",
        )
    )
    if unsupported:
        return "hard", "unsupported_region"

    if kind == "openai_api":
        valid_json = False
        if "json" in content_type:
            try:
                payload = json.loads(body.decode("utf-8"))
                serialized = json.dumps(payload, ensure_ascii=False).lower()
                valid_json = "bearer" in serialized or "authentication" in serialized
            except (UnicodeDecodeError, json.JSONDecodeError):
                valid_json = False
        if status == 401 and valid_json:
            return "healthy", "expected_401_json"
        if status in {429, 500, 502, 503, 504}:
            return "soft", f"upstream_http_{status}"
        return "hard", ("cloudflare_challenge" if challenged else f"unexpected_http_{status}")

    if kind == "openai_auth":
        valid_json = False
        if "json" in content_type:
            try:
                payload = json.loads(body.decode("utf-8"))
                issuer = str(payload.get("issuer") or "")
                valid_json = urlparse(issuer).hostname == "auth.openai.com"
            except (UnicodeDecodeError, json.JSONDecodeError):
                valid_json = False
        if status == 200 and valid_json:
            return "healthy", "expected_oidc_json"
        if status in {429, 500, 502, 503, 504}:
            return "soft", f"upstream_http_{status}"
        return "hard", ("cloudflare_challenge" if challenged else f"unexpected_http_{status}")

    if kind == "chatgpt_web":
        if status == 200 and not challenged:
            return "healthy", "http_200"
        if challenged:
            # 这是可达但受浏览器挑战影响，不能当健康，也不能单独触发切换。
            return "soft", "cloudflare_challenge"
        if status in {429, 500, 502, 503, 504}:
            return "soft", f"upstream_http_{status}"
        if status in {401, 403}:
            return "soft", f"access_http_{status}"
        return "hard", f"unexpected_http_{status}"

    if kind == "local":
        if 200 <= status < 400:
            return "healthy", f"http_{status}"
        return "soft", f"http_{status}"

    return (
        ("healthy", f"http_{status}")
        if 200 <= status < 400
        else (
            "hard",
            f"http_{status}",
        )
    )


def http_probe(
    proxy_url: str | None,
    probe: dict[str, Any],
) -> dict[str, Any]:
    timeout_seconds = int(probe["timeout_seconds"])
    command = [
        "/usr/bin/curl",
        "-sS",
        "--max-time",
        str(timeout_seconds),
        "--connect-timeout",
        str(min(timeout_seconds, int(probe.get("connect_timeout_seconds", 4)))),
        "--compressed",
        "-A",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/150 Safari/537.36",
        "-D",
        "-",
        "-o",
        "-",
        "-w",
        "\n__AIWATCH__ %{http_code} %{time_total}",
    ]
    if proxy_url:
        command.extend(["--proxy", proxy_url])
    else:
        command.extend(["--noproxy", "*"])
    command.append(probe["url"])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds + 3,
        )
        status, latency_ms, headers, body = parse_curl_output(completed.stdout)
        stderr = completed.stderr.decode("utf-8", errors="ignore")
        exit_code = int(completed.returncode)
    except (OSError, subprocess.TimeoutExpired):
        status, latency_ms, headers, body = 0, timeout_seconds * 1000, {}, b""
        stderr, exit_code = "subprocess timeout", 28
    verdict, reason = classify_probe(
        str(probe["kind"]),
        status,
        headers,
        body,
        exit_code,
        stderr,
    )
    return {
        "name": probe["name"],
        "kind": probe["kind"],
        "status": status,
        "latency_ms": latency_ms,
        "verdict": verdict,
        "reason": reason,
        "transport_ok": exit_code == 0 and status > 0,
        "content_type": headers.get("content-type", "")[:80],
        "cf_mitigated": headers.get("cf-mitigated", "")[:40],
    }


def route_probe(proxy_url: str, config: dict[str, Any]) -> dict[str, Any]:
    probes = list(config["active_probes"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as executor:
        results = list(executor.map(lambda item: http_probe(proxy_url, item), probes))
    by_name = {result["name"]: result for result in results}
    hard = [
        f"{result['name']}:{result['reason']}" for result in results if result["verdict"] == "hard"
    ]
    soft = [
        f"{result['name']}:{result['reason']}" for result in results if result["verdict"] == "soft"
    ]
    required_ok = all(
        by_name.get(name, {}).get("verdict") == "healthy" for name in ("openai_api", "openai_auth")
    )
    if hard:
        classification = "hard_failure"
    elif required_ok and soft:
        classification = "degraded"
    elif required_ok:
        classification = "healthy"
    else:
        classification = "soft_failure"
    latencies = [
        int(result["latency_ms"])
        for result in results
        if result["verdict"] == "healthy" and int(result["latency_ms"]) > 0
    ]
    return {
        "classification": classification,
        "usable": classification in {"healthy", "degraded"},
        "hard_reasons": hard,
        "soft_reasons": soft,
        "median_ms": int(statistics.median(latencies)) if latencies else None,
        "probes": {
            result["name"]: {
                "status": result["status"],
                "latency_ms": result["latency_ms"],
                "verdict": result["verdict"],
                "reason": result["reason"],
            }
            for result in results
        },
    }


def direct_network_probe(config: dict[str, Any]) -> dict[str, Any]:
    probes = list(config["direct_probes"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as executor:
        results = list(executor.map(lambda item: http_probe(None, item), probes))
    ok = any(result["verdict"] == "healthy" for result in results)
    return {
        "ok": ok,
        "results": {
            result["name"]: {
                "status": result["status"],
                "verdict": result["verdict"],
                "reason": result["reason"],
            }
            for result in results
        },
    }


def node_template(protocol: str = "unknown") -> dict[str, Any]:
    return {
        "protocol": protocol,
        "exit_ip": None,
        "exit_country": None,
        "exit_region": None,
        "asn": None,
        "as_organization": None,
        "openai_status": "unknown",
        "deep_verified_at": 0,
        "successes": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "latencies_ms": [],
        "median_ms": None,
        "last_success_at": 0,
        "last_failure_at": 0,
        "preflight_ok": False,
        "preflight_checked_at": 0,
        "preflight_latency_ms": None,
        "cooldown_until": 0,
        "needs_recovery": False,
        "recovery_successes": 0,
        "failure_events": [],
    }


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "monitor": {
            "last_seen_node": None,
            "expected_selection": None,
            "last_status": "unknown",
            "consecutive_hard_failures": 0,
            "hard_failure_streaks": {},
            "first_failure_at": 0,
            "failure_reasons": [],
            "last_switch_at": 0,
            "last_switch": None,
            "backoff_until": 0,
            "all_unavailable_episode": False,
            "all_unavailable_notified": False,
        },
        "nodes": {},
        "pools": {
            "active": [],
            "warm": [],
            "cold": [],
            "independent_exit_count": 0,
            "duplicate_exit_groups": 0,
            "rebuilt_at": 0,
        },
        "inventory": {
            "catalog_refreshed_at": 0,
            "deep_scanned_at": 0,
            "warm_scanned_at": 0,
            "cold_scanned_at": 0,
            "warm_cursor": 0,
            "cold_cursor": 0,
        },
        "switch_history": [],
    }


def migrate_legacy_state(loaded: dict[str, Any]) -> dict[str, Any]:
    state = default_state()
    monitor = state["monitor"]
    monitor["last_seen_node"] = loaded.get("last_seen_node")
    monitor["last_status"] = loaded.get("last_status", "unknown")
    monitor["last_switch_at"] = int(loaded.get("last_switch_at", 0) or 0)
    histories = loaded.get("history", {})
    if isinstance(histories, dict):
        for node, records in histories.items():
            entry = node_template()
            if isinstance(records, list):
                for item in records[-120:]:
                    if item.get("ok"):
                        entry["successes"] += 1
                        entry["last_success_at"] = max(
                            entry["last_success_at"], int(item.get("time", 0))
                        )
                    else:
                        entry["failures"] += 1
                    latency = item.get("worst_delay_ms")
                    if isinstance(latency, int) and latency > 0:
                        entry["latencies_ms"].append(latency)
            if entry["latencies_ms"]:
                entry["median_ms"] = int(statistics.median(entry["latencies_ms"]))
            state["nodes"][str(node)] = entry
    return state


def load_state(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()
    if not isinstance(loaded, dict):
        return default_state()
    if int(loaded.get("schema_version", 0)) != SCHEMA_VERSION:
        return migrate_legacy_state(loaded)
    state = default_state()
    state.update(loaded)
    for key, value in default_state()["monitor"].items():
        state.setdefault("monitor", {}).setdefault(key, value)
    for key, value in default_state()["pools"].items():
        state.setdefault("pools", {}).setdefault(key, value)
    for key, value in default_state()["inventory"].items():
        state.setdefault("inventory", {}).setdefault(key, value)
    state.setdefault("nodes", {})
    state.setdefault("switch_history", [])
    return state


def ensure_node(state: dict[str, Any], node: str, protocol: str = "unknown") -> dict[str, Any]:
    entry = state["nodes"].setdefault(node, node_template(protocol))
    for key, value in node_template(protocol).items():
        entry.setdefault(key, value)
    if protocol != "unknown":
        entry["protocol"] = protocol
    return entry


def success_rate(entry: dict[str, Any]) -> float:
    successes = int(entry.get("successes", 0))
    failures = int(entry.get("failures", 0))
    return (successes + 1) / (successes + failures + 2)


def update_route_observation(
    state: dict[str, Any],
    node: str,
    result: dict[str, Any],
    timestamp: int,
    deep: bool = False,
) -> None:
    entry = ensure_node(state, node)
    classification = result["classification"]
    if result.get("usable"):
        entry["successes"] = int(entry["successes"]) + 1
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = timestamp
        if classification == "healthy":
            entry["openai_status"] = "healthy"
        elif entry.get("openai_status") != "healthy":
            entry["openai_status"] = "degraded"
        latency = result.get("median_ms")
        if isinstance(latency, int) and latency > 0:
            values = [
                int(value)
                for value in entry.get("latencies_ms", [])
                if isinstance(value, int) and value > 0
            ]
            values.append(latency)
            entry["latencies_ms"] = values[-60:]
            entry["median_ms"] = int(statistics.median(entry["latencies_ms"]))
    elif classification == "hard_failure":
        entry["failures"] = int(entry["failures"]) + 1
        entry["consecutive_failures"] = int(entry["consecutive_failures"]) + 1
        entry["last_failure_at"] = timestamp
        entry["openai_status"] = "unavailable"
    if deep:
        entry["deep_verified_at"] = timestamp


def record_preflight(
    state: dict[str, Any],
    node: str,
    ok: bool,
    latency_ms: int | None,
    timestamp: int,
    config: dict[str, Any],
) -> None:
    entry = ensure_node(state, node)
    entry["preflight_ok"] = bool(ok)
    entry["preflight_checked_at"] = timestamp
    entry["preflight_latency_ms"] = latency_ms
    if ok and entry.get("needs_recovery") and timestamp >= int(entry.get("cooldown_until", 0)):
        entry["recovery_successes"] = int(entry.get("recovery_successes", 0)) + 1
        if entry["recovery_successes"] >= int(config["recovery_successes_required"]):
            entry["needs_recovery"] = False


def quarantine_node(
    state: dict[str, Any],
    node: str,
    config: dict[str, Any],
    timestamp: int,
) -> int:
    entry = ensure_node(state, node)
    window = int(config["repeat_failure_window_seconds"])
    events = [
        int(item) for item in entry.get("failure_events", []) if int(item) >= timestamp - window
    ]
    events.append(timestamp)
    entry["failure_events"] = events[-20:]
    duration = (
        int(config["cooldown_repeat_seconds"])
        if len(events) >= 2
        else int(config["cooldown_first_seconds"])
    )
    entry["cooldown_until"] = timestamp + duration
    entry["needs_recovery"] = True
    entry["recovery_successes"] = 0
    return duration


def is_real_proxy(item: dict[str, Any], exclude_pattern: re.Pattern[str]) -> bool:
    name = str(item.get("name") or "")
    proxy_type = str(item.get("type") or "")
    if not name or exclude_pattern.search(name):
        return False
    if proxy_type in {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}:
        return False
    if proxy_type in GROUP_TYPES:
        return False
    return bool(item.get("server") and item.get("port"))


def load_real_nodes(config: dict[str, Any]) -> dict[str, str]:
    path = Path(config["clash_generated_config_path"])
    document = safe_yaml_load(path.read_text(encoding="utf-8"))
    proxies = document.get("proxies", []) if isinstance(document, dict) else []
    exclude_pattern = re.compile(config["node_exclude_regex"], re.IGNORECASE)
    result: dict[str, str] = {}
    for item in proxies:
        if isinstance(item, dict) and is_real_proxy(item, exclude_pattern):
            result[str(item["name"])] = str(item.get("type") or "unknown")
    return result


def refresh_catalog(
    state: dict[str, Any], config: dict[str, Any], timestamp: int
) -> dict[str, str]:
    catalog = load_real_nodes(config)
    for name, protocol in catalog.items():
        ensure_node(state, name, protocol)
    for name in list(state["nodes"]):
        state["nodes"][name]["present_in_subscription"] = name in catalog
    state["inventory"]["catalog_refreshed_at"] = timestamp
    return catalog


def catalog_from_state(state: dict[str, Any]) -> dict[str, str]:
    return {
        name: str(entry.get("protocol") or "unknown")
        for name, entry in state["nodes"].items()
        if entry.get("present_in_subscription", True)
    }


def node_rank(entry: dict[str, Any], name: str) -> tuple[Any, ...]:
    return (
        0 if entry.get("openai_status") == "healthy" else 1,
        -success_rate(entry),
        -min(1000, int(entry.get("successes", 0)) + int(entry.get("failures", 0))),
        -int(entry.get("last_success_at", 0)),
        int(entry.get("median_ms") or 999999),
        name,
    )


def pool_eligible(entry: dict[str, Any], config: dict[str, Any], timestamp: int) -> bool:
    if entry.get("openai_status") not in {"healthy", "degraded"}:
        return False
    if not entry.get("exit_ip"):
        return False
    if int(entry.get("cooldown_until", 0)) > timestamp:
        return False
    if entry.get("needs_recovery"):
        return False
    verified = int(entry.get("deep_verified_at", 0))
    return verified >= timestamp - int(config["deep_verification_ttl_seconds"])


def rebuild_pools(
    state: dict[str, Any],
    catalog: dict[str, str],
    config: dict[str, Any],
    timestamp: int,
    current: str | None = None,
) -> None:
    representatives: dict[str, str] = {}
    duplicate_counts: dict[str, int] = {}
    for name in catalog:
        entry = ensure_node(state, name, catalog[name])
        if not pool_eligible(entry, config, timestamp):
            continue
        exit_ip = str(entry["exit_ip"])
        duplicate_counts[exit_ip] = duplicate_counts.get(exit_ip, 0) + 1
        previous = representatives.get(exit_ip)
        if previous is None or node_rank(entry, name) < node_rank(
            state["nodes"][previous], previous
        ):
            representatives[exit_ip] = name

    candidates = list(representatives.values())
    candidates.sort(key=lambda name: node_rank(state["nodes"][name], name))
    active_max = int(config["active_pool_max"])
    active: list[str] = []
    used_asn: set[str] = set()
    used_country: set[str] = set()

    if current in candidates:
        active.append(str(current))
        entry = state["nodes"][str(current)]
        used_asn.add(str(entry.get("asn") or ""))
        used_country.add(str(entry.get("exit_country") or ""))

    remaining = [name for name in candidates if name not in active]
    while remaining and len(active) < active_max:
        selected = min(
            remaining,
            key=lambda name: (
                1 if str(state["nodes"][name].get("asn") or "") in used_asn else 0,
                1 if str(state["nodes"][name].get("exit_country") or "") in used_country else 0,
                node_rank(state["nodes"][name], name),
            ),
        )
        remaining.remove(selected)
        active.append(selected)
        used_asn.add(str(state["nodes"][selected].get("asn") or ""))
        used_country.add(str(state["nodes"][selected].get("exit_country") or ""))

    warm = remaining[: int(config["warm_pool_max"])]
    chosen = set(active) | set(warm)
    cold = [name for name in catalog if name not in chosen]
    state["pools"] = {
        "active": active,
        "warm": warm,
        "cold": cold,
        "independent_exit_count": len(representatives),
        "duplicate_exit_groups": sum(1 for count in duplicate_counts.values() if count > 1),
        "rebuilt_at": timestamp,
    }


def preflight_nodes(
    controller: ClashController,
    nodes: Sequence[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    if not nodes:
        return []
    timeout_ms = int(config["candidate_preflight_timeout_ms"])
    url = config["candidate_preflight_url"]
    expected = str(config["candidate_preflight_expected_status"])

    def run(node: str) -> dict[str, Any]:
        delay = controller.delay(node, url, timeout_ms, expected)
        return {"node": node, "ok": delay is not None, "latency_ms": delay}

    workers = min(int(config["candidate_concurrency"]), len(nodes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        return list(executor.map(run, nodes))


def country_hint(name: str) -> str:
    match = re.match(r"^([^\dA-Z\-\s]+)", name)
    return match.group(1) if match else "其他"


def diversify_by_name(nodes: Sequence[str]) -> list[str]:
    buckets: dict[str, list[str]] = {}
    for node in nodes:
        buckets.setdefault(country_hint(node), []).append(node)
    result: list[str] = []
    while buckets:
        for key in sorted(list(buckets)):
            values = buckets[key]
            if values:
                result.append(values.pop(0))
            if not values:
                buckets.pop(key, None)
    return result


def find_free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def geo_probe(proxy_url: str, config: dict[str, Any]) -> dict[str, Any]:
    command = [
        "/usr/bin/curl",
        "-sS",
        "--max-time",
        str(int(config["geo_probe_timeout_seconds"])),
        "--connect-timeout",
        "4",
        "--proxy",
        proxy_url,
        "-A",
        "AI-Watchdog/2.0",
        config["geo_probe_url"],
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=int(config["geo_probe_timeout_seconds"]) + 3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False}
    if completed.returncode != 0:
        return {"ok": False}
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False}
    ip = str(payload.get("ip") or "")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return {"ok": False}
    asn_value = payload.get("asn")
    return {
        "ok": True,
        "exit_ip": ip,
        "exit_country": str(payload.get("country_code") or payload.get("country") or "")[:80],
        "exit_region": str(payload.get("country") or "")[:80],
        "asn": str(asn_value or "")[:40],
        "as_organization": str(
            payload.get("asn_organization")
            or payload.get("organization")
            or payload.get("isp")
            or ""
        )[:120],
    }


class IsolatedScanner:
    """使用第二个仅本机可见的 Mihomo，在内存配置上扫描，不动主 AI 组。"""

    def __init__(self, config: dict[str, Any], nodes: Sequence[str]):
        self.config = config
        self.nodes = list(nodes)
        self.port = find_free_port()
        self.secret = secrets.token_urlsafe(24)
        self.temp_dir: str | None = None
        self.socket_path: str | None = None
        self.process: subprocess.Popen[Any] | None = None
        self.controller: ClashController | None = None

    def __enter__(self) -> IsolatedScanner:
        live_path = Path(self.config["clash_generated_config_path"])
        live = safe_yaml_load(live_path.read_text(encoding="utf-8"))
        if not isinstance(live, dict):
            raise ScannerError("runtime_config_invalid")
        wanted = set(self.nodes)
        proxies = [
            item
            for item in live.get("proxies", [])
            if isinstance(item, dict) and str(item.get("name")) in wanted
        ]
        if not proxies:
            raise ScannerError("scanner_no_proxies")

        self.temp_dir = tempfile.mkdtemp(prefix="mihomo-ai-pool-scan.")
        os.chmod(self.temp_dir, 0o700)
        self.socket_path = str(Path(self.temp_dir) / "ctl.sock")
        document = {
            "mixed-port": self.port,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "silent",
            "ipv6": False,
            "external-controller": "",
            "external-controller-unix": self.socket_path,
            "secret": self.secret,
            "profile": {"store-selected": False},
            "dns": live.get("dns", {"enable": True, "ipv6": False}),
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": SCANNER_GROUP,
                    "type": "select",
                    "proxies": [str(item["name"]) for item in proxies],
                }
            ],
            "rules": [f"MATCH,{SCANNER_GROUP}"],
        }
        yaml_text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.lower()
            not in {
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
            }
        }
        try:
            self.process = subprocess.Popen(
                [
                    self.config["mihomo_core_path"],
                    "-d",
                    self.temp_dir,
                    "-f",
                    "/dev/stdin",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=environment,
            )
            assert self.process.stdin is not None
            self.process.stdin.write(yaml_text)
            self.process.stdin.close()
            self.process.stdin = None
        except (OSError, AssertionError) as exc:
            self.close()
            raise ScannerError(type(exc).__name__) from exc

        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.close()
                raise ScannerError("scanner_exited")
            if self.socket_path and Path(self.socket_path).exists():
                candidate = ClashController(self.socket_path, self.secret)
                try:
                    candidate.version()
                    self.controller = candidate
                    return self
                except ControllerError:
                    pass
            time.sleep(0.1)
        self.close()
        raise ScannerError("scanner_start_timeout")

    def scan(self, node: str) -> dict[str, Any]:
        if self.controller is None:
            raise ScannerError("scanner_not_started")
        self.controller.select(SCANNER_GROUP, node)
        time.sleep(0.15)
        proxy_url = f"http://127.0.0.1:{self.port}"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            route_future = executor.submit(route_probe, proxy_url, self.config)
            geo_future = executor.submit(geo_probe, proxy_url, self.config)
            route = route_future.result()
            geo = geo_future.result()
        return {"node": node, "route": route, "geo": geo}

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None
        if self.temp_dir:
            temporary = Path(self.temp_dir)
            if temporary.name.startswith("mihomo-ai-pool-scan."):
                shutil.rmtree(temporary, ignore_errors=True)
        self.temp_dir = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def apply_deep_scan(
    state: dict[str, Any],
    result: dict[str, Any],
    timestamp: int,
) -> None:
    node = result["node"]
    route = result["route"]
    geo = result["geo"]
    update_route_observation(state, node, route, timestamp, deep=True)
    entry = ensure_node(state, node)
    if geo.get("ok"):
        for key in (
            "exit_ip",
            "exit_country",
            "exit_region",
            "asn",
            "as_organization",
        ):
            entry[key] = geo.get(key)


def public_route_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "classification": result.get("classification"),
        "usable": result.get("usable"),
        "hard_reasons": result.get("hard_reasons", []),
        "soft_reasons": result.get("soft_reasons", []),
        "median_ms": result.get("median_ms"),
        "probes": result.get("probes", {}),
    }


def mark_all_unavailable(state: dict[str, Any], config: dict[str, Any], timestamp: int) -> bool:
    monitor = state["monitor"]
    should_notify = not bool(monitor.get("all_unavailable_notified"))
    monitor["all_unavailable_episode"] = True
    monitor["all_unavailable_notified"] = True
    monitor["backoff_until"] = timestamp + int(config["all_unavailable_backoff_seconds"])
    monitor["last_status"] = "all_unavailable"
    return should_notify


def clear_all_unavailable(state: dict[str, Any]) -> None:
    monitor = state["monitor"]
    monitor["all_unavailable_episode"] = False
    monitor["all_unavailable_notified"] = False
    monitor["backoff_until"] = 0


def candidate_order(
    state: dict[str, Any],
    candidates: Sequence[str],
    current: str,
) -> list[str]:
    current_entry = state["nodes"].get(current, {})
    current_ip = str(current_entry.get("exit_ip") or "")
    current_asn = str(current_entry.get("asn") or "")

    def key(name: str) -> tuple[Any, ...]:
        entry = state["nodes"].get(name, {})
        return (
            0 if entry.get("preflight_ok") else 1,
            0 if str(entry.get("exit_ip") or "") != current_ip else 1,
            0 if str(entry.get("asn") or "") != current_asn else 1,
            node_rank(entry, name),
        )

    ordered = sorted(set(candidates), key=key)
    result: list[str] = []
    seen_exit_ips = set()
    for name in ordered:
        exit_ip = str(state["nodes"].get(name, {}).get("exit_ip") or "")
        if exit_ip and exit_ip in seen_exit_ips:
            continue
        if exit_ip:
            seen_exit_ips.add(exit_ip)
        result.append(name)
    return result


def record_switch(
    state: dict[str, Any],
    old_node: str,
    new_node: str,
    reason: str,
    timestamp: int,
) -> None:
    event = {
        "time": timestamp,
        "old_node": old_node,
        "new_node": new_node,
        "reason": reason,
    }
    state["switch_history"].append(event)
    state["switch_history"] = state["switch_history"][-100:]
    state["monitor"]["last_switch_at"] = timestamp
    state["monitor"]["last_switch"] = event
    state["monitor"]["expected_selection"] = new_node


def failover(
    controller: ClashController,
    state: dict[str, Any],
    catalog: dict[str, str],
    config: dict[str, Any],
    log_path: Path,
    old_node: str,
    reason: str,
) -> dict[str, Any]:
    timestamp = now_ts()
    rebuild_pools(state, catalog, config, timestamp, old_node)
    attempted: list[str] = []
    selected_layers: list[str] = []
    suffixes = tuple(config.get("ai_domain_suffixes", DEFAULT_AI_SUFFIXES))
    max_attempts = int(config["max_candidate_attempts_per_failover"])

    layers = [
        ("active", list(state["pools"]["active"])),
        ("warm", list(state["pools"]["warm"])),
        ("cold", list(state["pools"]["cold"])),
    ]
    eligible_layers = [
        (
            layer_name,
            [
                node
                for node in layer_nodes
                if node != old_node
                and int(state["nodes"].get(node, {}).get("cooldown_until", 0)) <= timestamp
            ],
        )
        for layer_name, layer_nodes in layers
    ]
    remaining_attempts = max_attempts
    last_selected = old_node
    budget_exhausted = False

    for layer_index, (layer_name, layer_nodes) in enumerate(eligible_layers):
        if not layer_nodes:
            continue
        if remaining_attempts <= 0:
            budget_exhausted = True
            break
        preflight = preflight_nodes(controller, layer_nodes, config)
        checked_at = now_ts()
        for item in preflight:
            record_preflight(
                state,
                item["node"],
                bool(item["ok"]),
                item["latency_ms"],
                checked_at,
                config,
            )
        passing = [item["node"] for item in preflight if item["ok"]]
        ordered = candidate_order(state, passing, old_node)
        if ordered:
            selected_layers.append(layer_name)
        if len(ordered) > remaining_attempts:
            budget_exhausted = True

        for candidate in ordered:
            if remaining_attempts <= 0:
                break
            remaining_attempts -= 1
            attempted.append(candidate)
            try:
                controller.select(config["group_name"], candidate)
                last_selected = candidate
                state["monitor"]["expected_selection"] = candidate
            except ControllerError:
                quarantine_node(state, candidate, config, now_ts())
                continue

            time.sleep(float(config["switch_connection_wait_seconds"]))
            closed = controller.close_old_ai_connections(old_node, suffixes)
            verification = route_probe(config["mixed_proxy_url"], config)
            verified_at = now_ts()
            update_route_observation(state, candidate, verification, verified_at, deep=False)
            if verification.get("usable"):
                quarantine_node(state, old_node, config, verified_at)
                record_switch(state, old_node, candidate, reason, verified_at)
                monitor = state["monitor"]
                monitor["last_seen_node"] = candidate
                monitor["consecutive_hard_failures"] = 0
                monitor["hard_failure_streaks"] = {}
                monitor["first_failure_at"] = 0
                monitor["failure_reasons"] = []
                monitor["last_status"] = "healthy"
                clear_all_unavailable(state)
                rebuild_pools(state, catalog, config, verified_at, candidate)
                log_event(
                    log_path,
                    "switch_success",
                    old_node=old_node,
                    new_node=candidate,
                    reason=reason,
                    layer=layer_name,
                    closed_connections=closed,
                    verification=verification["classification"],
                )
                notify(
                    "OpenAI 代理已切换",
                    f"{old_node} → {candidate}；原因：{reason}",
                )
                return {
                    "ok": True,
                    "old_node": old_node,
                    "new_node": candidate,
                    "layer": layer_name,
                    "closed_connections": closed,
                    "verification": public_route_result(verification),
                }

            quarantine_node(state, candidate, config, verified_at)
            log_event(
                log_path,
                "candidate_verification_failed",
                candidate=candidate,
                layer=layer_name,
                reasons=verification.get("hard_reasons", [])
                or verification.get("soft_reasons", []),
            )
        if remaining_attempts <= 0:
            if any(nodes for _, nodes in eligible_layers[layer_index + 1 :]):
                budget_exhausted = True
            break

    if last_selected != old_node:
        try:
            controller.select(config["group_name"], old_node)
            state["monitor"]["expected_selection"] = old_node
        except ControllerError:
            pass

    if budget_exhausted:
        monitor = state["monitor"]
        monitor["backoff_until"] = now_ts() + int(config["candidate_retry_backoff_seconds"])
        monitor["last_status"] = "candidate_retry_backoff"
        log_event(
            log_path,
            "failover_attempt_budget_exhausted",
            old_node=old_node,
            attempted=attempted,
            layers=selected_layers,
            backoff_until=monitor["backoff_until"],
        )
        return {
            "ok": False,
            "reason": "attempt_budget_exhausted",
            "attempted": attempted,
            "layers": selected_layers,
            "notified": False,
            "backoff_until": monitor["backoff_until"],
        }

    should_notify = mark_all_unavailable(state, config, now_ts())
    log_event(
        log_path,
        "all_airport_unavailable",
        old_node=old_node,
        attempted=attempted,
        layers=selected_layers,
        notified=should_notify,
    )
    if should_notify:
        notify("AI 代理监控", "当前机场全部不可用")
    return {
        "ok": False,
        "reason": "all_unavailable",
        "attempted": attempted,
        "layers": selected_layers,
        "notified": should_notify,
    }


def current_group(controller: ClashController, group_name: str) -> tuple[str, list[str]]:
    group = controller.proxy(group_name)
    if not isinstance(group, dict):
        raise ControllerError("ai_group_missing")
    current = group.get("now")
    if not isinstance(current, str) or not current:
        raise ControllerError("ai_group_current_missing")
    members = [item for item in group.get("all", []) if isinstance(item, str)]
    return current, members


def health_iteration(
    controller: ClashController,
    state: dict[str, Any],
    catalog: dict[str, str],
    config: dict[str, Any],
    state_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    timestamp = now_ts()
    current, _ = current_group(controller, config["group_name"])
    monitor = state["monitor"]
    previous_seen = monitor.get("last_seen_node")
    expected = monitor.get("expected_selection")
    if previous_seen and previous_seen != current:
        if expected == current:
            monitor["expected_selection"] = None
        else:
            log_event(
                log_path,
                "manual_selection_detected",
                old_node=previous_seen,
                new_node=current,
            )
            # 手动选择不消耗切换次数、不触发冷却。
            monitor["consecutive_hard_failures"] = 0
            monitor["hard_failure_streaks"] = {}
            monitor["first_failure_at"] = 0
            monitor["failure_reasons"] = []
    monitor["last_seen_node"] = current
    ensure_node(state, current, catalog.get(current, "unknown"))

    result = route_probe(config["mixed_proxy_url"], config)
    update_route_observation(state, current, result, timestamp)

    if result.get("usable"):
        monitor["consecutive_hard_failures"] = 0
        monitor["hard_failure_streaks"] = {}
        monitor["first_failure_at"] = 0
        monitor["failure_reasons"] = []
        monitor["last_status"] = result["classification"]
        clear_all_unavailable(state)
        rebuild_pools(state, catalog, config, timestamp, current)
        atomic_write_json(state_path, state)
        return {
            "status": result["classification"],
            "current": current,
            "route": public_route_result(result),
        }

    if result["classification"] != "hard_failure":
        monitor["consecutive_hard_failures"] = 0
        monitor["hard_failure_streaks"] = {}
        monitor["first_failure_at"] = 0
        monitor["failure_reasons"] = []
        monitor["last_status"] = "soft_anomaly"
        log_event(
            log_path,
            "soft_anomaly",
            current=current,
            reasons=result.get("soft_reasons", []),
        )
        atomic_write_json(state_path, state)
        return {
            "status": "soft_anomaly",
            "current": current,
            "route": public_route_result(result),
        }

    direct = direct_network_probe(config)
    if not direct["ok"]:
        monitor["last_status"] = "local_network_down"
        monitor["consecutive_hard_failures"] = 0
        monitor["hard_failure_streaks"] = {}
        monitor["first_failure_at"] = 0
        monitor["failure_reasons"] = []
        log_event(log_path, "local_network_down", results=direct["results"])
        atomic_write_json(state_path, state)
        return {"status": "local_network_down", "direct": direct}

    hard_targets = {item.split(":", 1)[0] for item in result.get("hard_reasons", [])}
    previous_streaks = monitor.get("hard_failure_streaks", {})
    if not isinstance(previous_streaks, dict):
        previous_streaks = {}
    all_targets = {
        str(item.get("name")) for item in config.get("active_probes", []) if item.get("name")
    }
    streaks = {
        target: (int(previous_streaks.get(target, 0)) + 1 if target in hard_targets else 0)
        for target in all_targets
    }
    monitor["hard_failure_streaks"] = streaks
    failures = max(streaks.values(), default=0)
    monitor["consecutive_hard_failures"] = failures
    if failures == 1:
        monitor["first_failure_at"] = timestamp
        monitor["failure_reasons"] = list(result.get("hard_reasons", []))
    else:
        monitor["failure_reasons"] = list(result.get("hard_reasons", []))
    monitor["last_status"] = "hard_failure"
    log_event(
        log_path,
        "hard_failure",
        current=current,
        consecutive_failures=failures,
        reasons=result.get("hard_reasons", []),
    )

    if timestamp < int(monitor.get("backoff_until", 0)):
        atomic_write_json(state_path, state)
        return {
            "status": "backoff",
            "current": current,
            "backoff_until": monitor["backoff_until"],
        }

    required = int(config["failure_rounds_before_switch"])
    if failures < required:
        atomic_write_json(state_path, state)
        return {
            "status": "waiting_confirmation",
            "current": current,
            "consecutive_hard_failures": failures,
            "required": required,
            "route": public_route_result(result),
        }

    reason_values = [item.split(":", 1)[-1] for item in monitor.get("failure_reasons", [])]
    reason_text = "连续两次硬故障"
    if reason_values:
        reason_text += f"（{' + '.join(reason_values[:3])}）"
    outcome = failover(
        controller,
        state,
        catalog,
        config,
        log_path,
        current,
        reason_text,
    )
    atomic_write_json(state_path, state)
    return {
        "status": "switched" if outcome["ok"] else "all_unavailable",
        "result": outcome,
    }


class MaintenanceWorker(threading.Thread):
    def __init__(
        self,
        stop_event: threading.Event,
        state: dict[str, Any],
        state_lock: threading.Lock,
        state_path: Path,
        log_path: Path,
        config: dict[str, Any],
    ):
        super().__init__(name="ai-pool-maintenance", daemon=True)
        self.stop_event = stop_event
        self.state = state
        self.state_lock = state_lock
        self.state_path = state_path
        self.log_path = log_path
        self.config = config
        self.last_active_sweep = 0
        timestamp = now_ts()
        inventory = state.get("inventory", {})
        self.last_warm_scan = int(inventory.get("warm_scanned_at", 0) or timestamp)
        self.last_cold_scan = int(inventory.get("cold_scanned_at", 0) or timestamp)
        self.last_catalog_refresh = int(inventory.get("catalog_refreshed_at", 0) or timestamp)

    def snapshot_nodes(self, pool: str, count: int | None = None) -> list[str]:
        with self.state_lock:
            values = list(self.state["pools"].get(pool, []))
            cursor_key = f"{pool}_cursor"
            cursor = int(self.state["inventory"].get(cursor_key, 0))
            if not values:
                return []
            rotated = values[cursor:] + values[:cursor]
            selected = rotated if count is None else rotated[:count]
            self.state["inventory"][cursor_key] = (cursor + len(selected)) % len(values)
            return selected

    def apply_preflight(self, results: list[dict[str, Any]]) -> None:
        timestamp = now_ts()
        with self.state_lock:
            for item in results:
                record_preflight(
                    self.state,
                    item["node"],
                    bool(item["ok"]),
                    item["latency_ms"],
                    timestamp,
                    self.config,
                )
            catalog = catalog_from_state(self.state)
            current = self.state["monitor"].get("last_seen_node")
            rebuild_pools(
                self.state,
                catalog,
                self.config,
                timestamp,
                current,
            )
            atomic_write_json(self.state_path, self.state)

    def deep_scan(self, nodes: Sequence[str], label: str) -> None:
        if not nodes:
            return
        try:
            with IsolatedScanner(self.config, nodes) as scanner:
                for node in nodes:
                    if self.stop_event.is_set():
                        break
                    result = scanner.scan(node)
                    timestamp = now_ts()
                    with self.state_lock:
                        apply_deep_scan(self.state, result, timestamp)
                        catalog = catalog_from_state(self.state)
                        current = self.state["monitor"].get("last_seen_node")
                        rebuild_pools(
                            self.state,
                            catalog,
                            self.config,
                            timestamp,
                            current,
                        )
                        atomic_write_json(self.state_path, self.state)
                    log_event(
                        self.log_path,
                        "maintenance_deep_scan",
                        pool=label,
                        node=node,
                        classification=result["route"]["classification"],
                        exit_country=result["geo"].get("exit_country"),
                        asn=result["geo"].get("asn"),
                    )
        except (ScannerError, ControllerError, OSError) as exc:
            log_event(
                self.log_path,
                "maintenance_scanner_unavailable",
                pool=label,
                error=type(exc).__name__,
            )
        finally:
            with self.state_lock:
                self.state["inventory"][f"{label}_scanned_at"] = now_ts()
                atomic_write_json(self.state_path, self.state)

    def run(self) -> None:
        while not self.stop_event.wait(1):
            timestamp = now_ts()
            try:
                controller = controller_from_config(self.config)
            except (ControllerError, OSError):
                continue

            if timestamp - self.last_catalog_refresh >= int(
                self.config["catalog_refresh_interval_seconds"]
            ):
                with self.state_lock:
                    try:
                        catalog = refresh_catalog(self.state, self.config, timestamp)
                        current = self.state["monitor"].get("last_seen_node")
                        rebuild_pools(
                            self.state,
                            catalog,
                            self.config,
                            timestamp,
                            current,
                        )
                        atomic_write_json(self.state_path, self.state)
                    except (OSError, yaml.YAMLError):
                        pass
                self.last_catalog_refresh = timestamp

            if timestamp - self.last_active_sweep >= int(
                self.config["active_preflight_interval_seconds"]
            ):
                nodes = self.snapshot_nodes("active")
                results = preflight_nodes(controller, nodes, self.config)
                self.apply_preflight(results)
                self.last_active_sweep = now_ts()

            if timestamp - self.last_warm_scan >= int(self.config["warm_scan_interval_seconds"]):
                self.deep_scan(self.snapshot_nodes("warm", 1), "warm")
                self.last_warm_scan = now_ts()

            if timestamp - self.last_cold_scan >= int(self.config["cold_scan_interval_seconds"]):
                count = int(self.config["cold_scan_batch_size"])
                self.deep_scan(self.snapshot_nodes("cold", count), "cold")
                self.last_cold_scan = now_ts()


def command_status(config: dict[str, Any], state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    try:
        controller = controller_from_config(config)
        current, members = current_group(controller, config["group_name"])
        controller_status = "ok"
    except (ControllerError, OSError):
        current, members, controller_status = None, [], "unavailable"
    pools = state["pools"]
    current_entry = state["nodes"].get(str(current), {})
    return (0 if controller_status == "ok" else 2), {
        "controller": controller_status,
        "group": config["group_name"],
        "current": current,
        "group_member_count": len(members),
        "current_exit": {
            "country": current_entry.get("exit_country"),
            "asn": current_entry.get("asn"),
            "status": current_entry.get("openai_status", "unknown"),
        },
        "monitor": state["monitor"],
        "pools": {
            "active": len(pools.get("active", [])),
            "warm": len(pools.get("warm", [])),
            "cold": len(pools.get("cold", [])),
            "independent_exits": pools.get("independent_exit_count", 0),
            "duplicate_exit_groups": pools.get("duplicate_exit_groups", 0),
        },
        "catalog_nodes": sum(
            1 for item in state["nodes"].values() if item.get("present_in_subscription", True)
        ),
    }


def command_check(config: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    direct = direct_network_probe(config)
    try:
        controller = controller_from_config(config)
        current, _ = current_group(controller, config["group_name"])
        result = route_probe(config["mixed_proxy_url"], config)
    except (ControllerError, OSError) as exc:
        return 2, {
            "direct": direct,
            "controller": "unavailable",
            "error": type(exc).__name__,
        }
    return (0 if direct["ok"] and result["usable"] else 2), {
        "direct": direct,
        "current": current,
        "route": public_route_result(result),
    }


def command_inventory(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    log_path: Path,
) -> tuple[int, dict[str, Any]]:
    timestamp = now_ts()
    controller = controller_from_config(config)
    current, _ = current_group(controller, config["group_name"])
    catalog = refresh_catalog(state, config, timestamp)
    nodes = list(catalog)
    log_event(log_path, "inventory_preflight_started", nodes=len(nodes))
    preflight = preflight_nodes(controller, nodes, config)
    for item in preflight:
        record_preflight(
            state,
            item["node"],
            bool(item["ok"]),
            item["latency_ms"],
            now_ts(),
            config,
        )
    passing = [item["node"] for item in preflight if item["ok"]]
    failing = [item["node"] for item in preflight if not item["ok"]]
    previous_verified = [
        name for name in nodes if int(state["nodes"][name].get("deep_verified_at", 0)) > 0
    ]
    ordered = []
    for name in (
        [current] + diversify_by_name(passing) + previous_verified + diversify_by_name(failing)
    ):
        if name in catalog and name not in ordered:
            ordered.append(name)
    limit = min(int(config["initial_deep_scan_max"]), len(ordered))
    scanned = 0
    usable = 0
    errors = 0
    try:
        with IsolatedScanner(config, ordered[:limit]) as scanner:
            for node in ordered[:limit]:
                result = scanner.scan(node)
                scanned += 1
                if result["route"].get("usable"):
                    usable += 1
                apply_deep_scan(state, result, now_ts())
                rebuild_pools(state, catalog, config, now_ts(), current)
                atomic_write_json(state_path, state)
                pools = state["pools"]
                if len(pools["active"]) >= int(config["active_pool_max"]) and len(
                    pools["warm"]
                ) >= int(config["warm_pool_min"]):
                    break
    except (ScannerError, ControllerError, OSError) as exc:
        errors += 1
        log_event(
            log_path,
            "inventory_scanner_error",
            error=type(exc).__name__,
            scanned=scanned,
        )

    state["inventory"]["deep_scanned_at"] = now_ts()
    rebuild_pools(state, catalog, config, now_ts(), current)
    atomic_write_json(state_path, state)
    pools = state["pools"]
    result = {
        "catalog_nodes": len(catalog),
        "preflight_passed": len(passing),
        "deep_scanned": scanned,
        "deep_usable": usable,
        "active_pool": len(pools["active"]),
        "warm_pool": len(pools["warm"]),
        "cold_pool": len(pools["cold"]),
        "independent_exits": pools["independent_exit_count"],
        "duplicate_exit_groups": pools["duplicate_exit_groups"],
        "errors": errors,
    }
    log_event(log_path, "inventory_completed", **result)
    return (
        0 if len(pools["active"]) >= int(config["active_pool_min"]) else 2,
        result,
    )


def daemon_loop(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    log_path: Path,
) -> int:
    stop_event = threading.Event()

    def stop_handler(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    state_lock = threading.Lock()
    try:
        catalog = catalog_from_state(state)
        if not catalog:
            catalog = refresh_catalog(state, config, now_ts())
        rebuild_pools(
            state,
            catalog,
            config,
            now_ts(),
            state["monitor"].get("last_seen_node"),
        )
        atomic_write_json(state_path, state)
    except (OSError, yaml.YAMLError):
        catalog = {}

    maintenance = MaintenanceWorker(
        stop_event,
        state,
        state_lock,
        state_path,
        log_path,
        config,
    )
    maintenance.start()
    log_event(log_path, "daemon_started", interval=config["monitor_interval_seconds"])

    while not stop_event.is_set():
        iteration_started = time.monotonic()
        try:
            controller = controller_from_config(config)
            with state_lock:
                catalog = catalog_from_state(state)
                output = health_iteration(
                    controller,
                    state,
                    catalog,
                    config,
                    state_path,
                    log_path,
                )
            if output.get("status") in {
                "controller_unavailable",
                "all_unavailable",
            }:
                log_event(log_path, "daemon_iteration", status=output["status"])
        except (ControllerError, OSError) as exc:
            with state_lock:
                state["monitor"]["last_status"] = "controller_unavailable"
                atomic_write_json(state_path, state)
            log_event(
                log_path,
                "controller_unavailable",
                error=type(exc).__name__,
            )
        elapsed = time.monotonic() - iteration_started
        if elapsed > float(config["monitor_interval_seconds"]) * 1.5:
            log_event(
                log_path,
                "slow_health_iteration",
                elapsed_seconds=round(elapsed, 2),
            )
        remaining = max(0.2, float(config["monitor_interval_seconds"]) - elapsed)
        stop_event.wait(remaining)

    maintenance.join(timeout=8)
    log_event(log_path, "daemon_stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI 专用高可用监控")
    parser.add_argument(
        "command",
        choices=(
            "daemon",
            "run-once",
            "status",
            "check",
            "inventory",
        ),
        nargs="?",
        default="status",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def print_output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    runtime = Path(config["runtime_path"])
    state_path = runtime / "state.json"
    state = load_state(state_path)

    if args.command == "status":
        code, output = command_status(config, state)
        print_output(output)
        return code
    if args.command == "check":
        code, output = command_check(config)
        print_output(output)
        return code

    state_path, lock_path, log_path = ensure_runtime(config)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print_output({"status": "already_running"})
            return 0

        if args.command == "inventory":
            try:
                code, output = command_inventory(config, state, state_path, log_path)
            except (ControllerError, ScannerError, OSError, yaml.YAMLError) as exc:
                code, output = (
                    2,
                    {
                        "status": "inventory_failed",
                        "error": type(exc).__name__,
                    },
                )
            print_output(output)
            return code

        if args.command == "run-once":
            try:
                controller = controller_from_config(config)
                catalog = refresh_catalog(state, config, now_ts())
                output = health_iteration(
                    controller,
                    state,
                    catalog,
                    config,
                    state_path,
                    log_path,
                )
                code = 0 if output["status"] in {"healthy", "degraded", "switched"} else 2
            except (ControllerError, OSError, yaml.YAMLError) as exc:
                code, output = (
                    2,
                    {
                        "status": "controller_unavailable",
                        "error": type(exc).__name__,
                    },
                )
            print_output(output)
            return code

        return daemon_loop(config, state, state_path, log_path)


if __name__ == "__main__":
    sys.exit(main())
