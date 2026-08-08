"""Mihomo 的多 AI Provider 专用高可用监控。

设计边界：
- 只切换当前 Provider 的专用组，不改变全局节点、系统代理或 TUN。
- 当前节点健康时绝不因延迟变化切换。
- 只有连续两轮可验证硬故障才切换。
- 节点凭据只从 Clash 运行时配置读入内存，不写入本项目状态或日志。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fcntl
import hashlib
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
from contextlib import ExitStack, suppress
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
from .providers import ProviderError, enabled_provider_ids, resolve_provider_config

DEFAULT_CONFIG = default_config_path()
SCHEMA_VERSION = 3
SCANNER_GROUP = "🔬 AI出口扫描"
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Compatible"}
WEB_FEEDBACK_CONFIRMED = "confirmed"
WEB_FEEDBACK_REJECTED = "rejected"
WEB_FEEDBACK_STATUSES = {WEB_FEEDBACK_CONFIRMED, WEB_FEEDBACK_REJECTED}
MAINTENANCE_SCAN_SEMAPHORE = threading.Semaphore(1)
WEB_FEEDBACK_CONFIRMATION = "RECORD_WEB_FEEDBACK"


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
        exact_domains: Sequence[str] = (),
    ) -> int:
        """只关闭仍绑定旧节点的当前 Provider 连接。"""
        try:
            result = self.request("GET", "/connections")
        except ControllerError:
            return 0
        closed = 0
        for connection in result.get("connections", []):
            metadata = connection.get("metadata", {}) or {}
            host = str(metadata.get("host") or "").lower().rstrip(".")
            if not host_matches_targets(host, suffixes, exact_domains):
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

    def close_stale_ai_connections(
        self,
        new_node: str,
        suffixes: Sequence[str],
        exact_domains: Sequence[str] = (),
    ) -> int:
        """关闭所有仍绑定非新节点的 AI 连接，保留普通网站和新链路。"""
        try:
            result = self.request("GET", "/connections")
        except ControllerError:
            return 0
        closed = 0
        for connection in result.get("connections", []):
            metadata = connection.get("metadata", {}) or {}
            host = str(metadata.get("host") or "").lower().rstrip(".")
            if not host_matches_targets(host, suffixes, exact_domains):
                continue
            chains = [str(item) for item in connection.get("chains", [])]
            if new_node in chains:
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


def notify_all_unavailable_once(
    config: dict[str, Any],
    title: str,
    timestamp: int | None = None,
) -> bool:
    """Deduplicate the airport-wide Toast across Provider state machines."""

    shared_runtime = config.get("shared_runtime_path")
    if not shared_runtime:
        notify(title, "当前机场全部不可用")
        return True
    checked_at = now_ts() if timestamp is None else int(timestamp)
    root = Path(str(shared_runtime))
    lock_path = root / "all-unavailable-notification.lock"
    state_path = root / "all-unavailable-notification.json"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a+", encoding="utf-8") as lock:
            with suppress(OSError):
                os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                record = json.loads(state_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                record = {}
            last_notified_at = int(record.get("last_notified_at", 0) or 0)
            gate_seconds = max(300, int(config["all_unavailable_backoff_seconds"]))
            if checked_at - last_notified_at < gate_seconds:
                return False
            atomic_write_json(
                state_path,
                {
                    "last_notified_at": checked_at,
                    "provider_id": str(config.get("provider_id") or "openai"),
                },
            )
    except OSError:
        # The Provider episode itself still deduplicates notifications.  Do not
        # hide a real outage merely because the cross-Provider gate is unwritable.
        notify(title, "当前机场全部不可用")
        return True
    notify(title, "当前机场全部不可用")
    return True


def host_matches_suffixes(host: str, suffixes: Sequence[str]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in suffixes)


def host_matches_targets(
    host: str,
    suffixes: Sequence[str],
    exact_domains: Sequence[str] = (),
) -> bool:
    normalized = host.lower().rstrip(".")
    return normalized in {item.lower().rstrip(".") for item in exact_domains} or (
        host_matches_suffixes(normalized, suffixes)
    )


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
    probe: dict[str, Any] | None = None,
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

    if kind == "chatgpt_ws":
        # 未登录的根路径通常返回 404；这里验证的是 TCP/TLS/HTTP 传输路径，
        # 不把它冒充成已认证的 WebSocket 会话。
        if status in {200, 301, 302, 400, 401, 403, 404, 426}:
            return "healthy", f"transport_http_{status}"
        if status in {429, 500, 502, 503, 504}:
            return "soft", f"upstream_http_{status}"
        return "hard", f"unexpected_http_{status}"

    if kind == "generic_web":
        hard_statuses = {
            int(value) for value in (probe or {}).get("hard_statuses", []) if str(value).isdigit()
        }
        hard_markers = [
            str(value).lower()
            for value in (probe or {}).get("hard_body_markers", [])
            if str(value).strip()
        ]
        if status in hard_statuses:
            return "hard", f"configured_hard_http_{status}"
        if any(marker in text for marker in hard_markers):
            return "hard", "configured_unavailable_response"
        expected = {
            int(value)
            for value in (probe or {}).get("expected_statuses", [200, 301, 302])
            if str(value).isdigit()
        }
        if challenged:
            return "soft", "cloudflare_challenge"
        if status in expected:
            return "healthy", f"expected_http_{status}"
        if status in {401, 403}:
            return "soft", f"access_http_{status}"
        if status == 429 or 500 <= status <= 599:
            return "soft", f"upstream_http_{status}"
        return "hard", f"unexpected_http_{status}"

    if kind == "generic_transport":
        expected = {
            int(value)
            for value in (probe or {}).get(
                "expected_statuses", [200, 301, 302, 400, 401, 403, 404, 426]
            )
            if str(value).isdigit()
        }
        if status in expected:
            return "healthy", f"transport_http_{status}"
        if status == 429 or 500 <= status <= 599:
            return "soft", f"upstream_http_{status}"
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
        probe,
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


def route_probe(proxy_url: str | None, config: dict[str, Any]) -> dict[str, Any]:
    probes = list(config["active_probes"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(probes)) as executor:
        results = list(executor.map(lambda item: http_probe(proxy_url, item), probes))
    initial_hard_targets = {str(item["name"]) for item in results if item["verdict"] == "hard"}
    probes_by_name = {str(item["name"]): item for item in probes}
    results_by_name = {str(item["name"]): item for item in results}
    pending_retry = [
        probes_by_name[str(item["name"])] for item in results if item["verdict"] == "hard"
    ]
    for _ in range(int(config.get("hard_probe_retry_count", 0))):
        if not pending_retry:
            break
        retry_delay = float(config.get("hard_probe_retry_delay_seconds", 0))
        if retry_delay:
            time.sleep(retry_delay)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(pending_retry)) as executor:
            retried = list(executor.map(lambda item: http_probe(proxy_url, item), pending_retry))
        for item in retried:
            results_by_name[str(item["name"])] = item
        pending_retry = [
            probes_by_name[str(item["name"])] for item in retried if item["verdict"] == "hard"
        ]
    results = [results_by_name[str(item["name"])] for item in probes]
    final_hard_targets = {str(item["name"]) for item in results if item["verdict"] == "hard"}
    by_name = {result["name"]: result for result in results}
    hard = [
        f"{result['name']}:{result['reason']}" for result in results if result["verdict"] == "hard"
    ]
    soft = [
        f"{result['name']}:{result['reason']}" for result in results if result["verdict"] == "soft"
    ]
    required_names = [str(item) for item in config.get("required_probe_names", [])]
    if not required_names:
        required_names = ["openai_api", "openai_auth", "chatgpt_ws"]
    required_ok = all(by_name.get(name, {}).get("verdict") == "healthy" for name in required_names)
    ambiguous_names = {
        str(item) for item in config.get("browser_ambiguous_probe_names", ["chatgpt_web"])
    }
    soft_results = [result for result in results if result["verdict"] == "soft"]
    browser_challenge_only = bool(soft_results) and all(
        str(result["name"]) in ambiguous_names and result.get("reason") == "cloudflare_challenge"
        for result in soft_results
    )
    if hard:
        classification = "hard_failure"
    elif required_ok and browser_challenge_only:
        classification = "browser_ambiguous"
    elif required_ok and not soft:
        classification = "healthy"
    else:
        classification = "soft_unstable"
    latencies = [
        int(result["latency_ms"])
        for result in results
        if result["verdict"] == "healthy" and int(result["latency_ms"]) > 0
    ]
    return {
        "classification": classification,
        "usable": classification in {"healthy", "browser_ambiguous"},
        "candidate_eligible": classification in {"healthy", "browser_ambiguous"},
        "recovered_hard_targets": sorted(initial_hard_targets - final_hard_targets),
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
        "provider_status": "unknown",
        "last_observation": "unknown",
        "eligibility_state": "unknown",
        "candidate_eligible": False,
        "candidate_verified_at": 0,
        "candidate_samples": [],
        "last_full_probe_at": 0,
        "probe_stack_signature": None,
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
        "web_feedback": None,
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
            "last_hard_failure_at": 0,
            "first_failure_at": 0,
            "failure_reasons": [],
            "prepared_candidates": [],
            "failover_episode": None,
            "probation_until": 0,
            "probation_node": None,
            "probation_failures": 0,
            "last_healthy_at": 0,
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
            "active_full_scanned_at": 0,
            "refill_scanned_at": 0,
            "probe_stack_signature": None,
            "active_cursor": 0,
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


def migrate_v2_state(loaded: dict[str, Any]) -> dict[str, Any]:
    """保留 v2 的历史和出口信息，但废弃会导致池坍塌的旧健康结论。"""
    state = default_state()
    old_monitor = loaded.get("monitor", {})
    if isinstance(old_monitor, dict):
        for key in state["monitor"]:
            if key in old_monitor and key not in {
                "hard_failure_streaks",
                "prepared_candidates",
                "failover_episode",
                "probation_until",
                "probation_node",
                "probation_failures",
            }:
                state["monitor"][key] = old_monitor[key]
    state["monitor"]["consecutive_hard_failures"] = 0
    state["monitor"]["first_failure_at"] = 0
    state["monitor"]["failure_reasons"] = []
    state["monitor"]["last_status"] = "revalidation_required"

    old_nodes = loaded.get("nodes", {})
    if isinstance(old_nodes, dict):
        for name, value in old_nodes.items():
            entry = node_template()
            if isinstance(value, dict):
                entry.update(value)
            set_provider_status(entry, "unknown")
            entry["last_observation"] = "unknown"
            entry["eligibility_state"] = "unknown"
            entry["candidate_eligible"] = False
            entry["candidate_verified_at"] = 0
            entry["candidate_samples"] = []
            entry["last_full_probe_at"] = 0
            entry["probe_stack_signature"] = None
            if int(entry.get("cooldown_until", 0)) <= now_ts():
                entry["needs_recovery"] = False
                entry["recovery_successes"] = 0
            state["nodes"][str(name)] = entry

    old_inventory = loaded.get("inventory", {})
    if isinstance(old_inventory, dict):
        for key in state["inventory"]:
            if key in old_inventory:
                state["inventory"][key] = old_inventory[key]
    history = loaded.get("switch_history", [])
    if isinstance(history, list):
        state["switch_history"] = history[-100:]
    state["pools"]["cold"] = [
        name for name, entry in state["nodes"].items() if entry.get("present_in_subscription", True)
    ]
    return state


def load_state(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()
    if not isinstance(loaded, dict):
        return default_state()
    loaded_version = int(loaded.get("schema_version", 0))
    if loaded_version == 2:
        return migrate_v2_state(loaded)
    if loaded_version != SCHEMA_VERSION:
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
    for entry in state["nodes"].values():
        if not isinstance(entry, dict):
            continue
        if "provider_status" not in entry:
            entry["provider_status"] = str(entry.get("openai_status") or "unknown")
        for key, value in node_template().items():
            entry.setdefault(key, value)
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


def get_provider_status(entry: dict[str, Any]) -> str:
    return str(entry.get("provider_status") or entry.get("openai_status") or "unknown")


def set_provider_status(entry: dict[str, Any], status: str) -> None:
    entry["provider_status"] = status
    # Retain the v0.x state field so upgrades and third-party readers remain compatible.
    entry["openai_status"] = status


def recent_candidate_success_rate(
    entry: dict[str, Any], timestamp: int, window_seconds: int = 3600
) -> float:
    samples = [
        item
        for item in entry.get("candidate_samples", [])
        if isinstance(item, dict) and int(item.get("time", 0)) >= timestamp - window_seconds
    ]
    successes = sum(
        1 for item in samples if item.get("eligible") and item.get("retry_free") is True
    )
    return (successes + 1) / (len(samples) + 2)


def exit_fingerprint(entry: dict[str, Any]) -> dict[str, str | None] | None:
    """Return a normalized independent-exit identity, never a node-name identity."""
    raw_ip = str(entry.get("exit_ip") or "").strip()
    if not raw_ip:
        return None
    try:
        normalized_ip = str(ipaddress.ip_address(raw_ip))
    except ValueError:
        normalized_ip = raw_ip.lower()
    asn = str(entry.get("asn") or "").strip().upper() or None
    country = str(entry.get("exit_country") or "").strip().upper() or None
    return {
        "exit_ip": normalized_ip,
        "asn": asn,
        "exit_country": country,
    }


def active_web_feedback(
    entry: dict[str, Any],
    timestamp: int,
) -> dict[str, Any] | None:
    """Return unexpired browser feedback only while the exit identity still matches."""
    feedback = entry.get("web_feedback")
    if not isinstance(feedback, dict):
        return None
    if feedback.get("status") not in WEB_FEEDBACK_STATUSES:
        return None
    try:
        expires_at = int(feedback.get("expires_at", 0))
    except (TypeError, ValueError):
        return None
    if expires_at <= timestamp:
        return None
    recorded = feedback.get("exit_fingerprint")
    current_fingerprint = exit_fingerprint(entry)
    if not isinstance(recorded, dict) or current_fingerprint is None:
        return None
    if exit_fingerprint(recorded) != current_fingerprint:
        return None
    return feedback


def web_feedback_status(entry: dict[str, Any], timestamp: int) -> str:
    feedback = active_web_feedback(entry, timestamp)
    return str(feedback["status"]) if feedback else "unknown"


def record_web_feedback(
    state: dict[str, Any],
    node: str,
    status: str,
    timestamp: int,
    ttl_seconds: int,
    reason: str,
) -> dict[str, Any]:
    """Attach browser evidence to every node sharing the observed exit fingerprint."""
    if status not in WEB_FEEDBACK_STATUSES:
        raise ValueError("invalid_web_feedback_status")
    if ttl_seconds <= 0:
        raise ValueError("web_feedback_ttl_must_be_positive")
    if node not in state.get("nodes", {}):
        raise ValueError("web_feedback_node_not_found")
    source = ensure_node(state, node)
    fingerprint = exit_fingerprint(source)
    if fingerprint is None:
        raise ValueError("web_feedback_exit_fingerprint_required")

    expires_at = timestamp + ttl_seconds
    feedback = {
        "status": status,
        "observed_at": timestamp,
        "expires_at": expires_at,
        "exit_fingerprint": fingerprint,
        "reason": clean_text(reason or "manual_browser_validation", 120),
    }
    affected_nodes: list[str] = []
    for candidate, entry in state["nodes"].items():
        if exit_fingerprint(entry) != fingerprint:
            continue
        entry["web_feedback"] = dict(feedback)
        affected_nodes.append(candidate)
    return {
        "status": status,
        "observed_at": timestamp,
        "expires_at": expires_at,
        "affected_nodes": affected_nodes,
    }


def update_route_observation(
    state: dict[str, Any],
    node: str,
    result: dict[str, Any],
    timestamp: int,
    deep: bool = False,
    source: str = "live",
) -> None:
    entry = ensure_node(state, node)
    classification = result["classification"]
    candidate_eligible = bool(result.get("candidate_eligible"))
    retry_free = not bool(result.get("recovered_hard_targets"))
    entry["last_observation"] = classification
    if result.get("usable"):
        entry["successes"] = int(entry["successes"]) + 1
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = timestamp
        if classification == "healthy":
            set_provider_status(entry, "healthy")
        elif get_provider_status(entry) != "healthy":
            set_provider_status(entry, "browser_ambiguous")
        entry["eligibility_state"] = "ready"
        # 重试恢复仍是一次可用的完整路径样本；候选提交还需要至少一个
        # retry-free 深度样本，并在接管时通过更严格的在线门槛。
        entry["candidate_eligible"] = candidate_eligible
        if entry["candidate_eligible"]:
            entry["candidate_verified_at"] = timestamp
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
        entry["candidate_eligible"] = False
        if int(entry["consecutive_failures"]) >= 2:
            set_provider_status(entry, "unavailable")
            entry["eligibility_state"] = "unavailable"
        else:
            set_provider_status(entry, "suspect")
            entry["eligibility_state"] = "suspect"
    else:
        entry["candidate_eligible"] = False
        entry["eligibility_state"] = "suspect"
    if deep:
        entry["deep_verified_at"] = timestamp
        entry["last_full_probe_at"] = timestamp
        samples = [item for item in entry.get("candidate_samples", []) if isinstance(item, dict)]
        samples.append(
            {
                "time": timestamp,
                "eligible": candidate_eligible,
                "retry_free": retry_free,
                "classification": classification,
                "source": clean_text(source, 40),
            }
        )
        entry["candidate_samples"] = samples[-12:]


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
    # 浅层 /delay 只做低成本粗筛，不能证明完整 OpenAI 路径恢复。
    del config


def candidate_has_required_samples(
    entry: dict[str, Any],
    config: dict[str, Any],
    timestamp: int,
) -> bool:
    window_start = timestamp - int(config["candidate_validation_window_seconds"])
    samples = [
        item
        for item in entry.get("candidate_samples", [])
        if isinstance(item, dict)
        and bool(item.get("eligible"))
        and int(item.get("time", 0)) >= window_start
    ]
    required = int(config["candidate_validation_samples_required"])
    if len(samples) < required:
        return False
    selected = samples[-required:]
    minimum_gap = int(config["candidate_validation_min_gap_seconds"])
    if int(selected[-1]["time"]) - int(selected[0]["time"]) < minimum_gap:
        return False
    return any(item.get("retry_free") is True for item in selected) and int(
        selected[-1]["time"]
    ) >= timestamp - int(config["candidate_validation_fresh_seconds"])


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
    signature = runtime_probe_stack_signature(config)
    previous_signature = state["inventory"].get("probe_stack_signature")
    if previous_signature and signature and previous_signature != signature:
        for entry in state["nodes"].values():
            entry["candidate_eligible"] = False
            entry["candidate_verified_at"] = 0
            entry["candidate_samples"] = []
            entry["eligibility_state"] = "revalidation_required"
            entry["probe_stack_signature"] = None
    state["inventory"]["probe_stack_signature"] = signature
    for name, protocol in catalog.items():
        ensure_node(state, name, protocol)
    for name in list(state["nodes"]):
        state["nodes"][name]["present_in_subscription"] = name in catalog
    state["inventory"]["catalog_refreshed_at"] = timestamp
    return catalog


def runtime_probe_stack_signature(config: dict[str, Any]) -> str | None:
    """对会改变真实探针路径的 IPv6、DNS 和 hosts 设置生成本地摘要。"""
    try:
        document = safe_yaml_load(
            Path(config["clash_generated_config_path"]).read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    payload = {
        "ipv6": bool(document.get("ipv6", False)),
        "dns": document.get("dns", {}),
        "hosts": document.get("hosts", {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def catalog_from_state(state: dict[str, Any]) -> dict[str, str]:
    return {
        name: str(entry.get("protocol") or "unknown")
        for name, entry in state["nodes"].items()
        if entry.get("present_in_subscription", True)
    }


def node_rank(
    entry: dict[str, Any],
    name: str,
    timestamp: int | None = None,
) -> tuple[Any, ...]:
    checked_at = now_ts() if timestamp is None else timestamp
    feedback_status = web_feedback_status(entry, checked_at)
    return (
        0 if get_provider_status(entry) == "healthy" else 1,
        0
        if feedback_status == WEB_FEEDBACK_CONFIRMED
        else 2
        if feedback_status == WEB_FEEDBACK_REJECTED
        else 1,
        -recent_candidate_success_rate(entry, checked_at),
        -success_rate(entry),
        -min(1000, int(entry.get("successes", 0)) + int(entry.get("failures", 0))),
        -int(entry.get("last_success_at", 0)),
        int(entry.get("median_ms") or 999999),
        name,
    )


def pool_eligible(entry: dict[str, Any], config: dict[str, Any], timestamp: int) -> bool:
    if web_feedback_status(entry, timestamp) == WEB_FEEDBACK_REJECTED:
        return False
    if not entry.get("candidate_eligible"):
        return False
    if not entry.get("exit_ip"):
        return False
    if int(entry.get("cooldown_until", 0)) > timestamp:
        return False
    if entry.get("needs_recovery"):
        return False
    verified = int(entry.get("candidate_verified_at", 0))
    return verified >= timestamp - int(config["warm_candidate_ttl_seconds"])


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
        if previous is None or node_rank(entry, name, timestamp) < node_rank(
            state["nodes"][previous], previous, timestamp
        ):
            representatives[exit_ip] = name

    candidates = list(representatives.values())
    candidates.sort(key=lambda name: node_rank(state["nodes"][name], name, timestamp))
    active_cutoff = timestamp - int(config["active_candidate_ttl_seconds"])
    active_candidates = [
        name
        for name in candidates
        if int(state["nodes"][name].get("candidate_verified_at", 0)) >= active_cutoff
    ]
    active_max = int(config["active_pool_max"])
    active: list[str] = []
    used_asn: set[str] = set()
    used_country: set[str] = set()

    if current in active_candidates:
        active.append(str(current))
        entry = state["nodes"][str(current)]
        used_asn.add(str(entry.get("asn") or ""))
        used_country.add(str(entry.get("exit_country") or ""))

    remaining = [name for name in active_candidates if name not in active]
    while remaining and len(active) < active_max:
        selected = min(
            remaining,
            key=lambda name: (
                1 if str(state["nodes"][name].get("asn") or "") in used_asn else 0,
                1 if str(state["nodes"][name].get("exit_country") or "") in used_country else 0,
                node_rank(state["nodes"][name], name, timestamp),
            ),
        )
        remaining.remove(selected)
        active.append(selected)
        used_asn.add(str(state["nodes"][selected].get("asn") or ""))
        used_country.add(str(state["nodes"][selected].get("exit_country") or ""))

    warm_candidates = [name for name in candidates if name not in active]
    warm = warm_candidates[: int(config["warm_pool_max"])]
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
        self.stack_signature: str | None = None

    def __enter__(self) -> IsolatedScanner:
        live_path = Path(self.config["clash_generated_config_path"])
        live = safe_yaml_load(live_path.read_text(encoding="utf-8"))
        if not isinstance(live, dict):
            raise ScannerError("runtime_config_invalid")
        self.stack_signature = runtime_probe_stack_signature(self.config)
        wanted = set(self.nodes)
        proxies = [
            item
            for item in live.get("proxies", [])
            if isinstance(item, dict) and str(item.get("name")) in wanted
        ]
        if not proxies:
            raise ScannerError("scanner_no_proxies")

        dns_config = live.get("dns", {"enable": True, "ipv6": False})
        if isinstance(dns_config, dict):
            dns_config = dict(dns_config)
            # 隔离扫描器不继承可能占用系统端口的 DNS 监听地址。
            dns_config.pop("listen", None)

        self.temp_dir = tempfile.mkdtemp(prefix="mihomo-ai-pool-scan.")
        os.chmod(self.temp_dir, 0o700)
        self.socket_path = str(Path(self.temp_dir) / "ctl.sock")
        document = {
            "mixed-port": self.port,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "silent",
            "ipv6": bool(live.get("ipv6", False)),
            "external-controller": "",
            "external-controller-unix": self.socket_path,
            "secret": self.secret,
            "profile": {"store-selected": False},
            "dns": dns_config,
            "hosts": live.get("hosts", {}),
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
        return {
            "node": node,
            "route": route,
            "geo": geo,
            "probe_stack_signature": self.stack_signature,
        }

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
    config: dict[str, Any] | None = None,
    source: str = "isolated",
) -> None:
    node = result["node"]
    route = result["route"]
    geo = result["geo"]
    update_route_observation(
        state,
        node,
        route,
        timestamp,
        deep=True,
        source=source,
    )
    entry = ensure_node(state, node)
    if result.get("probe_stack_signature"):
        entry["probe_stack_signature"] = result["probe_stack_signature"]
    if geo.get("ok"):
        for key in (
            "exit_ip",
            "exit_country",
            "exit_region",
            "asn",
            "as_organization",
        ):
            entry[key] = geo.get(key)
    if (
        config is not None
        and route.get("candidate_eligible")
        and entry.get("needs_recovery")
        and timestamp >= int(entry.get("cooldown_until", 0))
    ):
        entry["recovery_successes"] = int(entry.get("recovery_successes", 0)) + 1
        if entry["recovery_successes"] >= int(config["recovery_successes_required"]):
            entry["needs_recovery"] = False
            entry["eligibility_state"] = "ready"


def _candidate_has_prior_sample(
    entry: dict[str, Any],
    config: dict[str, Any],
    timestamp: int,
) -> bool:
    earliest = timestamp - int(config["candidate_validation_window_seconds"])
    latest = timestamp - int(config["candidate_validation_min_gap_seconds"])
    return any(
        isinstance(item, dict)
        and bool(item.get("eligible"))
        and item.get("retry_free") is True
        and earliest <= int(item.get("time", 0)) <= latest
        for item in entry.get("candidate_samples", [])
    )


def _isolated_candidate_rounds(
    config: dict[str, Any],
    node: str,
    has_prior_sample: bool,
) -> dict[str, Any]:
    required = 1 if has_prior_sample else int(config["candidate_validation_samples_required"])
    observations: list[dict[str, Any]] = []
    try:
        with IsolatedScanner(config, [node]) as scanner:
            for index in range(required):
                result = scanner.scan(node)
                observations.append({"time": now_ts(), "result": result})
                route = result["route"]
                if not route.get("candidate_eligible"):
                    break
                if index + 1 < required:
                    time.sleep(float(config["candidate_validation_min_gap_seconds"]))
    except (ScannerError, ControllerError, OSError) as exc:
        return {
            "node": node,
            "observations": observations,
            "error": type(exc).__name__,
        }
    return {"node": node, "observations": observations, "error": None}


def prepare_failover_candidates(
    controller: ClashController,
    state: dict[str, Any],
    catalog: dict[str, str],
    config: dict[str, Any],
    log_path: Path,
    old_node: str,
    desired_count: int | None = None,
) -> list[dict[str, Any]]:
    """在不移动主 AI 组的情况下准备少量经过两轮验证的候选。"""
    timestamp = now_ts()
    rebuild_pools(state, catalog, config, timestamp, old_node)
    monitor = state["monitor"]
    episode = monitor.get("failover_episode")
    attempted_nodes: set[str] = set()
    attempted_exits: set[str] = set()
    if isinstance(episode, dict):
        attempted_nodes = {str(item) for item in episode.get("attempted_nodes", [])}
        attempted_exits = {str(item) for item in episode.get("attempted_exits", []) if item}

    prepared: list[dict[str, Any]] = []
    old_exit = str(state["nodes"].get(old_node, {}).get("exit_ip") or "")
    for item in monitor.get("prepared_candidates", []):
        if not isinstance(item, dict):
            continue
        node = str(item.get("node") or "")
        entry = state["nodes"].get(node, {})
        candidate_exit = str(entry.get("exit_ip") or "")
        if (
            node
            and node not in attempted_nodes
            and candidate_exit
            and (not old_exit or candidate_exit != old_exit)
            and int(entry.get("cooldown_until", 0)) <= timestamp
            and not entry.get("needs_recovery")
            and web_feedback_status(entry, timestamp) != WEB_FEEDBACK_REJECTED
            and timestamp - int(item.get("verified_at", 0))
            <= int(config["candidate_validation_fresh_seconds"])
            and candidate_has_required_samples(entry, config, timestamp)
        ):
            prepared.append(item)
    target_count = int(
        config["candidate_prepare_count"] if desired_count is None else desired_count
    )
    if len(prepared) >= target_count:
        return prepared[:target_count]

    layers = [
        ("active", list(state["pools"].get("active", []))),
        ("warm", list(state["pools"].get("warm", []))),
        ("cold", list(state["pools"].get("cold", []))),
    ]
    layer_by_node: dict[str, str] = {}
    candidates: list[str] = []
    seen_exits: set[str] = set(attempted_exits)
    for layer, nodes in layers:
        for node in candidate_order(state, nodes, old_node, timestamp):
            entry = state["nodes"].get(node, {})
            exit_ip = str(entry.get("exit_ip") or "")
            if (
                node == old_node
                or node in attempted_nodes
                or int(entry.get("cooldown_until", 0)) > timestamp
                or entry.get("needs_recovery")
                or web_feedback_status(entry, timestamp) == WEB_FEEDBACK_REJECTED
                or (exit_ip and exit_ip in seen_exits)
            ):
                continue
            candidates.append(node)
            layer_by_node[node] = layer
            if exit_ip:
                seen_exits.add(exit_ip)
            if len(candidates) >= int(config["candidate_prefilter_limit"]):
                break
        if len(candidates) >= int(config["candidate_prefilter_limit"]):
            break

    if not candidates:
        monitor["prepared_candidates"] = prepared
        return prepared

    log_event(
        log_path,
        "candidate_preparation_started",
        old_node=old_node,
        sampled=len(candidates),
    )
    preflight_fresh_seconds = max(
        int(config["active_preflight_interval_seconds"]) * 2,
        int(config["candidate_validation_fresh_seconds"]),
    )
    fresh_passing = [
        node
        for node in candidates
        if state["nodes"].get(node, {}).get("preflight_ok")
        and timestamp - int(state["nodes"].get(node, {}).get("preflight_checked_at", 0))
        <= preflight_fresh_seconds
    ]
    batch_size = int(config["candidate_prefilter_batch_size"])
    if attempted_nodes:
        batch_size *= 2
    needed = max(0, target_count - len(prepared))
    to_check = (
        []
        if len(fresh_passing) >= needed
        else [node for node in candidates if node not in fresh_passing][:batch_size]
    )
    preflight = preflight_nodes(controller, to_check, config)
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
        if not item["ok"]:
            record_episode_attempt(state, item["node"])
    passing = fresh_passing + [item["node"] for item in preflight if item["ok"]]
    ordered = candidate_order(state, passing, old_node, checked_at)
    wanted = [node for node in ordered if node not in {str(item.get("node")) for item in prepared}][
        : max(0, target_count - len(prepared))
    ]
    if not wanted:
        monitor["prepared_candidates"] = prepared
        return prepared

    workers = min(int(config["candidate_isolated_parallelism"]), len(wanted))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            node: executor.submit(
                _isolated_candidate_rounds,
                config,
                node,
                _candidate_has_prior_sample(state["nodes"].get(node, {}), config, checked_at),
            )
            for node in wanted
        }
        results = [futures[node].result() for node in wanted]

    for result in results:
        node = result["node"]
        for observation in result["observations"]:
            apply_deep_scan(
                state,
                observation["result"],
                int(observation["time"]),
                config,
                source="candidate_preflight",
            )
        entry = state["nodes"].get(node, {})
        verified_at = int(entry.get("candidate_verified_at", 0))
        candidate_exit = str(entry.get("exit_ip") or "")
        ready = result.get("error") is None and candidate_has_required_samples(
            entry, config, now_ts()
        )
        ready = ready and bool(candidate_exit) and (not old_exit or candidate_exit != old_exit)
        log_event(
            log_path,
            "candidate_preparation_result",
            node=node,
            layer=layer_by_node.get(node, "cold"),
            ready=ready,
            classification=entry.get("last_observation"),
            error=result.get("error"),
        )
        if ready:
            prepared.append(
                {
                    "node": node,
                    "layer": layer_by_node.get(node, "cold"),
                    "verified_at": verified_at,
                    "classification": entry.get("last_observation"),
                }
            )
        else:
            record_episode_attempt(state, node)

    monitor["prepared_candidates"] = prepared[:target_count]
    rebuild_pools(state, catalog, config, now_ts(), old_node)
    return list(monitor["prepared_candidates"])


def public_route_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "classification": result.get("classification"),
        "usable": result.get("usable"),
        "candidate_eligible": result.get("candidate_eligible"),
        "recovered_hard_targets": result.get("recovered_hard_targets", []),
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
    timestamp: int | None = None,
) -> list[str]:
    checked_at = now_ts() if timestamp is None else timestamp
    current_entry = state["nodes"].get(current, {})
    current_ip = str(current_entry.get("exit_ip") or "")
    current_asn = str(current_entry.get("asn") or "")

    def key(name: str) -> tuple[Any, ...]:
        entry = state["nodes"].get(name, {})
        feedback = web_feedback_status(entry, checked_at)
        return (
            0 if entry.get("preflight_ok") else 1,
            0 if get_provider_status(entry) == "healthy" else 1,
            0
            if feedback == WEB_FEEDBACK_CONFIRMED
            else 2
            if feedback == WEB_FEEDBACK_REJECTED
            else 1,
            -recent_candidate_success_rate(entry, checked_at),
            0 if str(entry.get("exit_ip") or "") != current_ip else 1,
            0 if str(entry.get("asn") or "") != current_asn else 1,
            -success_rate(entry),
            -int(entry.get("last_success_at", 0)),
            int(entry.get("median_ms") or 999999),
            name,
        )

    eligible = {
        name
        for name in candidates
        if web_feedback_status(state["nodes"].get(name, {}), checked_at) != WEB_FEEDBACK_REJECTED
    }
    ordered = sorted(eligible, key=key)
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


def ensure_failover_episode(
    state: dict[str, Any],
    current: str,
    timestamp: int,
) -> dict[str, Any]:
    monitor = state["monitor"]
    episode = monitor.get("failover_episode")
    if not isinstance(episode, dict) or episode.get("phase") == "complete":
        episode = {
            "id": f"{timestamp}-{abs(hash(current)) % 100000}",
            "started_at": timestamp,
            "old_node": current,
            "phase": "confirming",
            "attempted_nodes": [],
            "attempted_exits": [],
        }
        monitor["failover_episode"] = episode
    return episode


def record_episode_attempt(state: dict[str, Any], node: str) -> None:
    episode = state["monitor"].get("failover_episode")
    if not isinstance(episode, dict):
        return
    attempted = [str(item) for item in episode.get("attempted_nodes", [])]
    if node not in attempted:
        attempted.append(node)
    episode["attempted_nodes"] = attempted
    exit_ip = str(state["nodes"].get(node, {}).get("exit_ip") or "")
    exits = [str(item) for item in episode.get("attempted_exits", []) if item]
    if exit_ip and exit_ip not in exits:
        exits.append(exit_ip)
    episode["attempted_exits"] = exits


def clear_failure_confirmation(monitor: dict[str, Any]) -> None:
    monitor["consecutive_hard_failures"] = 0
    monitor["hard_failure_streaks"] = {}
    monitor["last_hard_failure_at"] = 0
    monitor["first_failure_at"] = 0
    monitor["failure_reasons"] = []
    monitor["prepared_candidates"] = []


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
    episode = ensure_failover_episode(state, old_node, timestamp)
    episode["phase"] = "switching"
    attempted: list[str] = []
    suffixes = tuple(config.get("ai_domain_suffixes", DEFAULT_AI_SUFFIXES))
    exact_domains = tuple(config.get("ai_exact_domains", ()))
    provider_name = str(config.get("provider_display_name") or "AI")
    max_attempts = int(config["max_candidate_attempts_per_failover"])
    prepared = prepare_failover_candidates(
        controller,
        state,
        catalog,
        config,
        log_path,
        old_node,
        desired_count=max_attempts,
    )
    last_selected = old_node
    selected_attempts = 0
    for item in prepared:
        if selected_attempts >= max_attempts:
            break
        candidate = str(item["node"])
        layer_name = str(item.get("layer") or "cold")
        attempted.append(candidate)
        record_episode_attempt(state, candidate)
        commit_delay = controller.delay(
            candidate,
            config["candidate_preflight_url"],
            int(config["candidate_commit_preflight_timeout_ms"]),
            str(config["candidate_preflight_expected_status"]),
        )
        record_preflight(
            state,
            candidate,
            commit_delay is not None,
            commit_delay,
            now_ts(),
            config,
        )
        if commit_delay is None:
            quarantine_node(state, candidate, config, now_ts())
            log_event(
                log_path,
                "candidate_commit_preflight_failed",
                episode_id=episode["id"],
                candidate=candidate,
                layer=layer_name,
            )
            continue
        try:
            controller.select(config["group_name"], candidate)
            selected_attempts += 1
            last_selected = candidate
            state["monitor"]["expected_selection"] = candidate
        except ControllerError:
            quarantine_node(state, candidate, config, now_ts())
            continue

        time.sleep(float(config["switch_connection_wait_seconds"]))
        verification = route_probe(config["mixed_proxy_url"], config)
        verified_at = now_ts()
        update_route_observation(
            state,
            candidate,
            verification,
            verified_at,
            deep=True,
            source="live_verification",
        )
        initial_recovered_targets = list(verification.get("recovered_hard_targets", []))
        if verification.get("candidate_eligible") and initial_recovered_targets:
            log_event(
                log_path,
                "candidate_reverification_required",
                episode_id=episode["id"],
                candidate=candidate,
                layer=layer_name,
                recovered_hard_targets=initial_recovered_targets,
            )
            time.sleep(float(config["candidate_reverification_delay_seconds"]))
            verification = route_probe(config["mixed_proxy_url"], config)
            verified_at = now_ts()
            update_route_observation(
                state,
                candidate,
                verification,
                verified_at,
                deep=True,
                source="live_reverification",
            )
        clean_verification = bool(verification.get("candidate_eligible")) and not bool(
            verification.get("recovered_hard_targets")
        )
        if clean_verification:
            quarantine_node(state, old_node, config, verified_at)
            record_switch(state, old_node, candidate, reason, verified_at)
            monitor = state["monitor"]
            monitor["last_seen_node"] = candidate
            clear_failure_confirmation(monitor)
            monitor["last_status"] = verification["classification"]
            monitor["last_healthy_at"] = verified_at
            monitor["probation_until"] = verified_at + int(config["probation_seconds"])
            monitor["probation_node"] = candidate
            monitor["probation_failures"] = 0
            episode["phase"] = "probation"
            episode["new_node"] = candidate
            clear_all_unavailable(state)
            closed = (
                controller.close_stale_ai_connections(candidate, suffixes, exact_domains)
                if exact_domains
                else controller.close_stale_ai_connections(candidate, suffixes)
            )
            rebuild_pools(state, catalog, config, verified_at, candidate)
            log_event(
                log_path,
                "switch_success",
                episode_id=episode["id"],
                old_node=old_node,
                new_node=candidate,
                reason=reason,
                layer=layer_name,
                closed_connections=closed,
                verification=verification["classification"],
                probation_until=monitor["probation_until"],
            )
            notify(
                f"{provider_name} 代理已切换",
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
            episode_id=episode["id"],
            candidate=candidate,
            layer=layer_name,
            reasons=verification.get("hard_reasons", []) or verification.get("soft_reasons", []),
            recovered_hard_targets=verification.get("recovered_hard_targets", []),
            initial_recovered_hard_targets=initial_recovered_targets,
        )
        try:
            controller.select(config["group_name"], old_node)
            state["monitor"]["expected_selection"] = old_node
            last_selected = old_node
        except ControllerError:
            pass

    if last_selected != old_node:
        try:
            controller.select(config["group_name"], old_node)
            state["monitor"]["expected_selection"] = old_node
        except ControllerError:
            pass

    episode_attempted = {str(item) for item in episode.get("attempted_nodes", [])}
    episode_exits = {str(item) for item in episode.get("attempted_exits", []) if item}
    remaining = []
    for node in catalog:
        if node == old_node or node in episode_attempted:
            continue
        entry = state["nodes"].get(node, {})
        exit_ip = str(entry.get("exit_ip") or "")
        if exit_ip and exit_ip in episode_exits:
            continue
        if web_feedback_status(entry, now_ts()) == WEB_FEEDBACK_REJECTED:
            continue
        remaining.append(node)

    if remaining:
        monitor = state["monitor"]
        monitor["backoff_until"] = now_ts() + int(config["candidate_retry_backoff_seconds"])
        monitor["last_status"] = "candidate_retry_backoff"
        episode["phase"] = "searching"
        log_event(
            log_path,
            "failover_attempt_budget_exhausted",
            episode_id=episode["id"],
            old_node=old_node,
            attempted=attempted,
            remaining=len(remaining),
            backoff_until=monitor["backoff_until"],
        )
        return {
            "ok": False,
            "reason": "candidates_not_ready",
            "attempted": attempted,
            "notified": False,
            "backoff_until": monitor["backoff_until"],
        }

    episode_should_notify = mark_all_unavailable(state, config, now_ts())
    should_notify = bool(
        episode_should_notify and notify_all_unavailable_once(config, f"{provider_name} 代理监控")
    )
    episode["phase"] = "all_unavailable"
    log_event(
        log_path,
        "all_airport_unavailable",
        old_node=old_node,
        attempted=attempted,
        episode_id=episode["id"],
        notified=should_notify,
    )
    return {
        "ok": False,
        "reason": "all_unavailable",
        "attempted": attempted,
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
            clear_failure_confirmation(monitor)
            monitor["failover_episode"] = None
            monitor["probation_until"] = 0
            monitor["probation_node"] = None
            monitor["probation_failures"] = 0
    monitor["last_seen_node"] = current
    ensure_node(state, current, catalog.get(current, "unknown"))

    result = route_probe(config["mixed_proxy_url"], config)
    if result.get("recovered_hard_targets"):
        log_event(
            log_path,
            "hard_probe_retry_recovered",
            targets=result["recovered_hard_targets"],
        )
    update_route_observation(state, current, result, timestamp)

    if result.get("usable"):
        clear_failure_confirmation(monitor)
        monitor["last_status"] = result["classification"]
        monitor["last_healthy_at"] = timestamp
        probation_active = (
            monitor.get("probation_node") == current
            and int(monitor.get("probation_until", 0)) > timestamp
        )
        if monitor.get("probation_node") == current and not probation_active:
            episode = monitor.get("failover_episode")
            if isinstance(episode, dict):
                episode["phase"] = "complete"
                log_event(
                    log_path,
                    "switch_probation_passed",
                    episode_id=episode.get("id"),
                    current=current,
                )
            monitor["failover_episode"] = None
            monitor["probation_until"] = 0
            monitor["probation_node"] = None
            monitor["probation_failures"] = 0
        elif not probation_active:
            monitor["failover_episode"] = None
        clear_all_unavailable(state)
        rebuild_pools(state, catalog, config, timestamp, current)
        atomic_write_json(state_path, state)
        return {
            "status": result["classification"],
            "current": current,
            "route": public_route_result(result),
        }

    if result["classification"] != "hard_failure":
        clear_failure_confirmation(monitor)
        if not int(monitor.get("probation_until", 0)):
            monitor["failover_episode"] = None
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
        clear_failure_confirmation(monitor)
        if not int(monitor.get("probation_until", 0)):
            monitor["failover_episode"] = None
        log_event(log_path, "local_network_down", results=direct["results"])
        atomic_write_json(state_path, state)
        return {"status": "local_network_down", "direct": direct}

    hard_targets = {item.split(":", 1)[0] for item in result.get("hard_reasons", [])}
    previous_streaks = monitor.get("hard_failure_streaks", {})
    if not isinstance(previous_streaks, dict):
        previous_streaks = {}
    monitor["hard_failure_streaks"] = {
        target: int(previous_streaks.get(target, 0)) + 1 for target in hard_targets
    }
    previous_failure_at = int(monitor.get("last_hard_failure_at", 0))
    previous_failures = int(monitor.get("consecutive_hard_failures", 0))
    minimum_gap = int(config["failure_confirmation_min_gap_seconds"])
    if previous_failure_at and timestamp - previous_failure_at < minimum_gap:
        failures = max(1, previous_failures)
    else:
        failures = previous_failures + 1 if previous_failures else 1
        monitor["last_hard_failure_at"] = timestamp
    monitor["consecutive_hard_failures"] = failures
    if failures == 1:
        monitor["first_failure_at"] = timestamp
        ensure_failover_episode(state, current, timestamp)
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
        if not bool(config.get("parallel_failure_confirmation")) or required != 2:
            prepare_failover_candidates(
                controller,
                state,
                catalog,
                config,
                log_path,
                current,
            )
            atomic_write_json(state_path, state)
            return {
                "status": "waiting_confirmation",
                "current": current,
                "consecutive_hard_failures": failures,
                "required": required,
                "route": public_route_result(result),
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            preparation = executor.submit(
                prepare_failover_candidates,
                controller,
                state,
                catalog,
                config,
                log_path,
                current,
            )
            time.sleep(float(minimum_gap))
            confirmation = route_probe(config["mixed_proxy_url"], config)
            confirmation_direct = (
                direct_network_probe(config)
                if confirmation["classification"] == "hard_failure"
                else None
            )
            try:
                preparation.result()
            except (ControllerError, ScannerError, OSError, yaml.YAMLError) as exc:
                log_event(
                    log_path,
                    "candidate_preparation_failed",
                    error=type(exc).__name__,
                )

        confirmation_timestamp = now_ts()
        selected_after_confirmation, _ = current_group(controller, config["group_name"])
        if selected_after_confirmation != current:
            clear_failure_confirmation(monitor)
            monitor["failover_episode"] = None
            monitor["last_seen_node"] = selected_after_confirmation
            monitor["last_status"] = "manual_selection_detected"
            atomic_write_json(state_path, state)
            return {
                "status": "manual_selection_detected",
                "current": selected_after_confirmation,
            }

        update_route_observation(
            state,
            current,
            confirmation,
            confirmation_timestamp,
        )
        if confirmation.get("usable"):
            clear_failure_confirmation(monitor)
            if not int(monitor.get("probation_until", 0)):
                monitor["failover_episode"] = None
            monitor["last_status"] = confirmation["classification"]
            monitor["last_healthy_at"] = confirmation_timestamp
            clear_all_unavailable(state)
            rebuild_pools(state, catalog, config, confirmation_timestamp, current)
            atomic_write_json(state_path, state)
            return {
                "status": confirmation["classification"],
                "current": current,
                "route": public_route_result(confirmation),
            }
        if confirmation["classification"] != "hard_failure":
            clear_failure_confirmation(monitor)
            if not int(monitor.get("probation_until", 0)):
                monitor["failover_episode"] = None
            monitor["last_status"] = "soft_anomaly"
            log_event(
                log_path,
                "soft_anomaly",
                current=current,
                reasons=confirmation.get("soft_reasons", []),
            )
            atomic_write_json(state_path, state)
            return {
                "status": "soft_anomaly",
                "current": current,
                "route": public_route_result(confirmation),
            }
        if confirmation_direct is not None and not confirmation_direct["ok"]:
            monitor["last_status"] = "local_network_down"
            clear_failure_confirmation(monitor)
            if not int(monitor.get("probation_until", 0)):
                monitor["failover_episode"] = None
            log_event(
                log_path,
                "local_network_down",
                results=confirmation_direct["results"],
            )
            atomic_write_json(state_path, state)
            return {"status": "local_network_down", "direct": confirmation_direct}

        result = confirmation
        timestamp = confirmation_timestamp
        failures = 2
        monitor["consecutive_hard_failures"] = failures
        monitor["last_hard_failure_at"] = timestamp
        monitor["hard_failure_streaks"] = {
            target: int(monitor["hard_failure_streaks"].get(target, 0)) + 1
            for target in {item.split(":", 1)[0] for item in result.get("hard_reasons", [])}
        }
        monitor["failure_reasons"] = list(result.get("hard_reasons", []))
        monitor["last_status"] = "hard_failure"
        log_event(
            log_path,
            "hard_failure",
            current=current,
            consecutive_failures=failures,
            reasons=result.get("hard_reasons", []),
        )

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
        "status": (
            "switched"
            if outcome["ok"]
            else "all_unavailable"
            if outcome.get("reason") == "all_unavailable"
            else "candidate_retry_backoff"
        ),
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
        provider_id = str(config.get("provider_id") or "openai")
        super().__init__(name=f"ai-pool-maintenance-{provider_id}", daemon=True)
        self.stop_event = stop_event
        self.state = state
        self.state_lock = state_lock
        self.state_path = state_path
        self.log_path = log_path
        self.config = config
        self.last_active_sweep = 0
        timestamp = now_ts()
        inventory = state.get("inventory", {})
        self.last_active_full_scan = int(inventory.get("active_full_scanned_at", 0) or timestamp)
        self.last_refill_scan = 0
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
        if not MAINTENANCE_SCAN_SEMAPHORE.acquire(blocking=False):
            log_event(
                self.log_path,
                "maintenance_scan_deferred",
                pool=label,
                reason="another_provider_scan_active",
            )
            return
        try:
            with IsolatedScanner(self.config, nodes) as scanner:
                for node in nodes:
                    if self.stop_event.is_set():
                        break
                    result = scanner.scan(node)
                    timestamp = now_ts()
                    with self.state_lock:
                        apply_deep_scan(
                            self.state,
                            result,
                            timestamp,
                            self.config,
                            source=f"maintenance_{label}",
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
            MAINTENANCE_SCAN_SEMAPHORE.release()
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

            with self.state_lock:
                monitor = self.state.get("monitor", {})
                episode = monitor.get("failover_episode")
                episode_phase = episode.get("phase") if isinstance(episode, dict) else None
                maintenance_paused = bool(
                    int(monitor.get("consecutive_hard_failures", 0))
                    or int(monitor.get("probation_until", 0)) > timestamp
                    or episode_phase in {"confirming", "switching", "searching"}
                )
            if maintenance_paused:
                continue

            if timestamp - self.last_active_sweep >= int(
                self.config["active_preflight_interval_seconds"]
            ):
                nodes = self.snapshot_nodes(
                    "active",
                    int(self.config["active_preflight_batch_size"]),
                )
                results = preflight_nodes(controller, nodes, self.config)
                self.apply_preflight(results)
                self.last_active_sweep = now_ts()

            if timestamp - self.last_active_full_scan >= int(
                self.config["active_full_scan_interval_seconds"]
            ):
                count = int(self.config["active_full_scan_batch_size"])
                self.deep_scan(self.snapshot_nodes("active", count), "active_full")
                self.last_active_full_scan = now_ts()

            with self.state_lock:
                active_short = len(self.state["pools"].get("active", [])) < int(
                    self.config["active_pool_min"]
                )
                warm_short = len(self.state["pools"].get("warm", [])) < int(
                    self.config["warm_pool_min"]
                )
            if (active_short or warm_short) and timestamp - self.last_refill_scan >= int(
                self.config["pool_refill_interval_seconds"]
            ):
                count = int(self.config["pool_refill_batch_size"])
                self.deep_scan(self.snapshot_nodes("cold", count), "refill")
                self.last_refill_scan = now_ts()

            if timestamp - self.last_warm_scan >= int(self.config["warm_scan_interval_seconds"]):
                count = int(self.config["warm_scan_batch_size"])
                self.deep_scan(self.snapshot_nodes("warm", count), "warm")
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
        "provider_id": config.get("provider_id", "openai"),
        "provider": config.get("provider_display_name", "OpenAI / ChatGPT / Codex"),
        "controller": controller_status,
        "group": config["group_name"],
        "current": current,
        "group_member_count": len(members),
        "current_exit": {
            "country": current_entry.get("exit_country"),
            "asn": current_entry.get("asn"),
            "status": get_provider_status(current_entry),
            "web_feedback": web_feedback_status(current_entry, now_ts()),
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
    identity = {
        "provider_id": config.get("provider_id", "openai"),
        "provider": config.get("provider_display_name", "OpenAI / ChatGPT / Codex"),
    }
    try:
        controller = controller_from_config(config)
        current, _ = current_group(controller, config["group_name"])
        result = route_probe(config["mixed_proxy_url"], config)
    except (ControllerError, OSError) as exc:
        return 2, {
            **identity,
            "direct": direct,
            "controller": "unavailable",
            "error": type(exc).__name__,
        }
    return (0 if direct["ok"] and result["usable"] else 2), {
        **identity,
        "direct": direct,
        "current": current,
        "route": public_route_result(result),
    }


def command_web_feedback(
    config: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    log_path: Path,
    node: str,
    status: str,
    reason: str,
    ttl_seconds: int | None = None,
) -> tuple[int, dict[str, Any]]:
    timestamp = now_ts()
    ttl_key = (
        "web_feedback_confirmed_ttl_seconds"
        if status == WEB_FEEDBACK_CONFIRMED
        else "web_feedback_rejected_ttl_seconds"
    )
    ttl = int(config[ttl_key]) if ttl_seconds is None else int(ttl_seconds)
    result = record_web_feedback(
        state,
        node,
        status,
        timestamp,
        ttl,
        reason,
    )
    catalog = catalog_from_state(state)
    rebuild_pools(
        state,
        catalog,
        config,
        timestamp,
        state["monitor"].get("last_seen_node"),
    )
    atomic_write_json(state_path, state)
    log_event(
        log_path,
        "web_feedback_recorded",
        node=node,
        status=status,
        reason=clean_text(reason, 120),
        expires_at=result["expires_at"],
        affected_nodes=len(result["affected_nodes"]),
    )
    return 0, {
        "status": "recorded",
        "node": node,
        "web_feedback": status,
        "expires_at": result["expires_at"],
        "affected_nodes": len(result["affected_nodes"]),
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
                apply_deep_scan(
                    state,
                    result,
                    now_ts(),
                    config,
                    source="inventory",
                )
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
    *,
    stop_event: threading.Event | None = None,
    manage_signals: bool = True,
) -> int:
    active_stop_event = threading.Event() if stop_event is None else stop_event

    def stop_handler(signum: int, frame: Any) -> None:
        del signum, frame
        active_stop_event.set()

    if manage_signals:
        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
    state_lock = threading.Lock()
    try:
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
        active_stop_event,
        state,
        state_lock,
        state_path,
        log_path,
        config,
    )
    maintenance.start()
    log_event(
        log_path,
        "daemon_started",
        provider_id=config.get("provider_id", "openai"),
        interval=config["monitor_interval_seconds"],
    )

    while not active_stop_event.is_set():
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
        active_stop_event.wait(remaining)

    maintenance.join(timeout=8)
    log_event(log_path, "daemon_stopped")
    return 0


def daemon_supervisor(config: dict[str, Any]) -> int:
    """Run one isolated failover state machine per enabled Provider."""

    provider_ids = enabled_provider_ids(config)
    if not provider_ids:
        print_output({"status": "no_enabled_providers"})
        return 2
    stop_event = threading.Event()

    def stop_handler(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    threads: list[threading.Thread] = []
    provider_failed = False
    with ExitStack() as stack:
        runtimes: list[tuple[dict[str, Any], dict[str, Any], Path, Path]] = []
        for provider_id in provider_ids:
            provider_config = resolve_provider_config(config, provider_id)
            state_path, lock_path, log_path = ensure_runtime(provider_config)
            lock = stack.enter_context(lock_path.open("a+", encoding="utf-8"))
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print_output(
                    {
                        "status": "already_running",
                        "provider_id": provider_id,
                    }
                )
                stop_event.set()
                return 2
            runtimes.append(
                (
                    provider_config,
                    load_state(state_path),
                    state_path,
                    log_path,
                )
            )

        for index, (provider_config, state, state_path, log_path) in enumerate(runtimes):
            provider_id = str(provider_config["provider_id"])
            thread = threading.Thread(
                target=daemon_loop,
                name=f"ai-provider-{provider_id}",
                args=(provider_config, state, state_path, log_path),
                kwargs={"stop_event": stop_event, "manage_signals": False},
                daemon=False,
            )
            thread.start()
            threads.append(thread)
            if index + 1 < len(runtimes) and stop_event.wait(1.5):
                break

        while not stop_event.wait(1):
            if any(not thread.is_alive() for thread in threads):
                provider_failed = True
                stop_event.set()
                break
        for thread in threads:
            thread.join(timeout=12)
    return 2 if provider_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多 AI Provider 专用高可用监控")
    parser.add_argument(
        "command",
        choices=(
            "daemon",
            "run-once",
            "status",
            "check",
            "inventory",
            "web-feedback",
        ),
        nargs="?",
        default="status",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--provider")
    parser.add_argument("--node")
    parser.add_argument("--web-status", choices=tuple(sorted(WEB_FEEDBACK_STATUSES)))
    parser.add_argument("--reason", default="manual_browser_validation")
    parser.add_argument("--ttl-seconds", type=int)
    parser.add_argument("--confirm", default="")
    return parser


def print_output(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_config = load_config(args.config)
    provider_id: str
    if args.command == "daemon" and args.provider is None:
        providers = enabled_provider_ids(base_config)
        if not providers:
            print_output({"status": "no_enabled_providers"})
            return 2
        if len(providers) > 1:
            return daemon_supervisor(base_config)
        provider_id = providers[0]
    else:
        provider_id = str(args.provider or base_config.get("default_provider_id") or "openai")
    if args.command == "daemon" and not bool(
        base_config.get("providers", {}).get(provider_id, {}).get("enabled")
    ):
        print_output({"status": "provider_disabled", "provider_id": provider_id})
        return 2
    try:
        config = resolve_provider_config(base_config, provider_id)
    except ProviderError as exc:
        print_output({"status": "provider_invalid", "error": str(exc)})
        return 2
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
            return 2 if args.command == "web-feedback" else 0

        if args.command == "web-feedback":
            if args.confirm != WEB_FEEDBACK_CONFIRMATION:
                print_output(
                    {
                        "status": "confirmation_required",
                        "confirmation": WEB_FEEDBACK_CONFIRMATION,
                    }
                )
                return 2
            if not args.node or not args.web_status:
                print_output({"status": "node_and_web_status_required"})
                return 2
            try:
                code, output = command_web_feedback(
                    config,
                    state,
                    state_path,
                    log_path,
                    args.node,
                    args.web_status,
                    args.reason,
                    args.ttl_seconds,
                )
            except ValueError as exc:
                code, output = 2, {"status": str(exc)}
            print_output(output)
            return code
        elif args.command == "inventory":
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
                code = 0 if output["status"] in {"healthy", "browser_ambiguous", "switched"} else 2
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
