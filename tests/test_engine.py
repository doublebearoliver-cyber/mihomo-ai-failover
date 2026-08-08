import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mihomo_ai_failover import engine as WATCHDOG
from mihomo_ai_failover.config import default_config


def load_settings():
    return default_config(home=Path("/tmp/mihomo-ai-failover-tests"))


def healthy_route(latency=300):
    return {
        "classification": "healthy",
        "usable": True,
        "candidate_eligible": True,
        "recovered_hard_targets": [],
        "hard_reasons": [],
        "soft_reasons": [],
        "median_ms": latency,
        "probes": {},
    }


class FakeController:
    def __init__(self, current="节点B"):
        self.current = current
        self.selections = []
        self.deleted = []

    def proxies(self):
        return {
            "🤖 AI稳定出口": {
                "now": self.current,
                "all": ["节点A", "节点B"],
            }
        }

    def proxy(self, name):
        return self.proxies().get(name, {})

    def select(self, group, node):
        self.selections.append((group, node))
        self.current = node

    def delay(self, node, url, timeout_ms, expected):
        del node, url, timeout_ms, expected
        return None

    def close_stale_ai_connections(self, new_node, suffixes):
        del new_node, suffixes
        return 0


class ProbeClassificationTests(unittest.TestCase):
    def test_openai_401_json_is_healthy(self):
        verdict, reason = WATCHDOG.classify_probe(
            "openai_api",
            401,
            {"content-type": "application/json"},
            b'{"error":{"message":"Missing bearer authentication"}}',
            0,
            "",
        )
        self.assertEqual((verdict, reason), ("healthy", "expected_401_json"))

    def test_openai_challenge_is_hard(self):
        verdict, reason = WATCHDOG.classify_probe(
            "openai_api",
            403,
            {
                "content-type": "text/html",
                "cf-mitigated": "challenge",
            },
            b"<html></html>",
            0,
            "",
        )
        self.assertEqual(verdict, "hard")
        self.assertEqual(reason, "cloudflare_challenge")

    def test_chatgpt_challenge_is_soft_not_healthy(self):
        verdict, reason = WATCHDOG.classify_probe(
            "chatgpt_web",
            403,
            {
                "content-type": "text/html",
                "cf-mitigated": "challenge",
            },
            b"<html></html>",
            0,
            "",
        )
        self.assertEqual(verdict, "soft")
        self.assertEqual(reason, "cloudflare_challenge")

    def test_chatgpt_ws_404_proves_transport(self):
        verdict, reason = WATCHDOG.classify_probe(
            "chatgpt_ws",
            404,
            {"server": "cloudflare"},
            b"",
            0,
            "",
        )
        self.assertEqual((verdict, reason), ("healthy", "transport_http_404"))

    def test_generic_web_can_classify_configured_region_response_as_hard(self):
        verdict, reason = WATCHDOG.classify_probe(
            "generic_web",
            200,
            {"content-type": "text/html"},
            b"This service is not available for this account region",
            0,
            "",
            {
                "expected_statuses": [200],
                "hard_body_markers": ["not available for this account region"],
            },
        )
        self.assertEqual((verdict, reason), ("hard", "configured_unavailable_response"))

    def test_browser_challenge_is_separate_candidate_state(self):
        config = load_settings()

        def fake_probe(proxy_url, probe):
            del proxy_url
            values = {
                "openai_api": ("healthy", "expected_401_json", 401),
                "openai_auth": ("healthy", "expected_oidc_json", 200),
                "chatgpt_web": ("soft", "cloudflare_challenge", 403),
                "chatgpt_ws": ("healthy", "transport_http_404", 404),
            }
            verdict, reason, status = values[probe["name"]]
            return {
                "name": probe["name"],
                "kind": probe["kind"],
                "status": status,
                "latency_ms": 100,
                "verdict": verdict,
                "reason": reason,
            }

        with mock.patch.object(WATCHDOG, "http_probe", side_effect=fake_probe):
            result = WATCHDOG.route_probe("http://127.0.0.1:7897", config)
        self.assertEqual(result["classification"], "browser_ambiguous")
        self.assertEqual(result["recovered_hard_targets"], [])
        self.assertTrue(result["candidate_eligible"])

    def test_generic_soft_web_failure_is_not_a_candidate(self):
        config = load_settings()

        def fake_probe(proxy_url, probe):
            del proxy_url
            verdict = "soft" if probe["name"] == "chatgpt_web" else "healthy"
            reason = "upstream_http_503" if probe["name"] == "chatgpt_web" else "expected_transport"
            return {
                "name": probe["name"],
                "kind": probe["kind"],
                "status": 503 if verdict == "soft" else 200,
                "latency_ms": 100,
                "verdict": verdict,
                "reason": reason,
            }

        with mock.patch.object(WATCHDOG, "http_probe", side_effect=fake_probe):
            result = WATCHDOG.route_probe("http://127.0.0.1:7897", config)
        self.assertEqual(result["classification"], "soft_unstable")
        self.assertFalse(result["candidate_eligible"])

    def test_hard_probe_is_retried_before_the_round_counts_as_failure(self):
        config = load_settings()
        calls = {}
        lock = threading.Lock()

        def fake_probe(proxy_url, probe):
            del proxy_url
            name = probe["name"]
            with lock:
                calls[name] = calls.get(name, 0) + 1
                attempt = calls[name]
            if name == "openai_api" and attempt == 1:
                verdict, reason, status = "hard", "timeout", 0
            elif name == "chatgpt_web":
                verdict, reason, status = "soft", "cloudflare_challenge", 403
            else:
                verdict, reason, status = "healthy", "expected_transport", 200
            return {
                "name": name,
                "kind": probe["kind"],
                "status": status,
                "latency_ms": 100,
                "verdict": verdict,
                "reason": reason,
            }

        with (
            mock.patch.object(WATCHDOG, "http_probe", side_effect=fake_probe),
            mock.patch.object(WATCHDOG.time, "sleep"),
        ):
            result = WATCHDOG.route_probe("http://127.0.0.1:7897", config)
        self.assertEqual(result["classification"], "browser_ambiguous")
        self.assertEqual(result["recovered_hard_targets"], ["openai_api"])
        self.assertEqual(calls["openai_api"], 2)
        self.assertEqual(calls["openai_auth"], 1)

    def test_repeated_hard_probe_still_counts_as_hard(self):
        config = load_settings()
        calls = {}

        def fake_probe(proxy_url, probe):
            del proxy_url
            name = probe["name"]
            calls[name] = calls.get(name, 0) + 1
            if name == "openai_api":
                verdict, reason, status = "hard", "timeout", 0
            elif name == "chatgpt_web":
                verdict, reason, status = "soft", "cloudflare_challenge", 403
            else:
                verdict, reason, status = "healthy", "expected_transport", 200
            return {
                "name": name,
                "kind": probe["kind"],
                "status": status,
                "latency_ms": 100,
                "verdict": verdict,
                "reason": reason,
            }

        with (
            mock.patch.object(WATCHDOG, "http_probe", side_effect=fake_probe),
            mock.patch.object(WATCHDOG.time, "sleep"),
        ):
            result = WATCHDOG.route_probe("http://127.0.0.1:7897", config)
        self.assertEqual(result["classification"], "hard_failure")
        self.assertEqual(result["recovered_hard_targets"], [])
        self.assertEqual(calls["openai_api"], 2)

    def test_unsupported_region_is_hard(self):
        verdict, reason = WATCHDOG.classify_probe(
            "openai_api",
            403,
            {"content-type": "application/json"},
            b'{"error":{"code":"unsupported_country"}}',
            0,
            "",
        )
        self.assertEqual((verdict, reason), ("hard", "unsupported_region"))

    def test_timeout_is_hard(self):
        verdict, reason = WATCHDOG.classify_probe(
            "openai_auth", 0, {}, b"", 28, "Operation timed out"
        )
        self.assertEqual((verdict, reason), ("hard", "timeout"))


class MultiProviderResourceTests(unittest.TestCase):
    def test_all_unavailable_toast_is_deduplicated_across_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            config = default_config(home=Path(directory), clash_root=Path(directory) / "clash")
            openai = WATCHDOG.resolve_provider_config(config, "openai")
            kimi = WATCHDOG.resolve_provider_config(config, "kimi")
            with mock.patch.object(WATCHDOG, "notify") as notify:
                first = WATCHDOG.notify_all_unavailable_once(openai, "OpenAI", timestamp=1000)
                second = WATCHDOG.notify_all_unavailable_once(kimi, "Kimi", timestamp=1001)

        self.assertTrue(first)
        self.assertFalse(second)
        notify.assert_called_once_with("OpenAI", "当前机场全部不可用")

    def test_schema_three_openai_state_keeps_health_on_provider_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                '{"schema_version":3,"nodes":{"node":{"openai_status":"healthy"}}}',
                encoding="utf-8",
            )
            loaded = WATCHDOG.load_state(state_path)

        self.assertEqual(loaded["nodes"]["node"]["provider_status"], "healthy")
        self.assertEqual(WATCHDOG.get_provider_status(loaded["nodes"]["node"]), "healthy")

    def test_background_deep_scan_defers_while_another_provider_scan_is_active(self):
        config = load_settings()
        config["provider_id"] = "kimi"
        state = WATCHDOG.default_state()
        worker = WATCHDOG.MaintenanceWorker(
            threading.Event(),
            state,
            threading.Lock(),
            Path("/tmp/unused-provider-state.json"),
            Path("/tmp/unused-provider-log.jsonl"),
            config,
        )

        WATCHDOG.MAINTENANCE_SCAN_SEMAPHORE.acquire()
        try:
            with (
                mock.patch.object(WATCHDOG, "IsolatedScanner") as scanner,
                mock.patch.object(WATCHDOG, "log_event") as log_event,
            ):
                worker.deep_scan(["节点A"], "warm")
        finally:
            WATCHDOG.MAINTENANCE_SCAN_SEMAPHORE.release()

        scanner.assert_not_called()
        log_event.assert_called_once_with(
            Path("/tmp/unused-provider-log.jsonl"),
            "maintenance_scan_deferred",
            pool="warm",
            reason="another_provider_scan_active",
        )

    def test_single_enabled_non_default_provider_is_selected_for_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = default_config(home=root, clash_root=root / "clash")
            config["providers"]["openai"]["enabled"] = False
            config["providers"]["kimi"]["enabled"] = True
            with (
                mock.patch.object(WATCHDOG, "load_config", return_value=config),
                mock.patch.object(WATCHDOG, "daemon_loop", return_value=0) as daemon,
            ):
                result = WATCHDOG.main(["daemon", "--config", str(root / "config.yaml")])

        self.assertEqual(result, 0)
        selected = daemon.call_args.args[0]
        self.assertEqual(selected["provider_id"], "kimi")

    def test_explicitly_disabled_provider_daemon_is_rejected(self):
        config = load_settings()
        config["providers"]["kimi"]["enabled"] = False
        with (
            mock.patch.object(WATCHDOG, "load_config", return_value=config),
            mock.patch.object(WATCHDOG, "daemon_loop") as daemon,
        ):
            result = WATCHDOG.main(["daemon", "--provider", "kimi"])

        self.assertEqual(result, 2)
        daemon.assert_not_called()


class DomainAndConnectionTests(unittest.TestCase):
    def test_ai_suffixes_do_not_capture_development_services(self):
        suffixes = load_settings()["ai_domain_suffixes"]
        self.assertTrue(WATCHDOG.host_matches_suffixes("auth.openai.com", suffixes))
        self.assertTrue(
            WATCHDOG.host_matches_suffixes("sdmntprwestus3.oaiusercontent.com", suffixes)
        )
        self.assertFalse(WATCHDOG.host_matches_suffixes("github.com", suffixes))
        self.assertFalse(WATCHDOG.host_matches_suffixes("registry.npmjs.org", suffixes))
        self.assertFalse(WATCHDOG.host_matches_suffixes("google.com", suffixes))

    def test_close_only_old_ai_connections(self):
        calls = []

        class Controller(WATCHDOG.ClashController):
            def __init__(self):
                pass

            def request(self, method, path, payload=None, timeout=15):
                del payload, timeout
                if method == "GET":
                    return {
                        "connections": [
                            {
                                "id": "old-ai",
                                "metadata": {"host": "chatgpt.com"},
                                "chains": ["旧节点", "🤖 AI稳定出口"],
                            },
                            {
                                "id": "new-ai",
                                "metadata": {"host": "api.openai.com"},
                                "chains": ["新节点", "🤖 AI稳定出口"],
                            },
                            {
                                "id": "github",
                                "metadata": {"host": "github.com"},
                                "chains": ["旧节点", "🔰 节点选择"],
                            },
                        ]
                    }
                calls.append((method, path))
                return {}

        closed = Controller().close_old_ai_connections(
            "旧节点", load_settings()["ai_domain_suffixes"]
        )
        self.assertEqual(closed, 1)
        self.assertEqual(calls, [("DELETE", "/connections/old-ai")])

    def test_close_all_stale_ai_connections_but_keep_new_and_github(self):
        calls = []

        class Controller(WATCHDOG.ClashController):
            def __init__(self):
                pass

            def request(self, method, path, payload=None, timeout=15):
                del payload, timeout
                if method == "GET":
                    return {
                        "connections": [
                            {
                                "id": "old-ai",
                                "metadata": {"host": "chatgpt.com"},
                                "chains": ["旧节点", "🤖 AI稳定出口"],
                            },
                            {
                                "id": "older-ai",
                                "metadata": {"host": "ws.chatgpt.com"},
                                "chains": ["更旧节点", "🤖 AI稳定出口"],
                            },
                            {
                                "id": "new-ai",
                                "metadata": {"host": "api.openai.com"},
                                "chains": ["新节点", "🤖 AI稳定出口"],
                            },
                            {
                                "id": "github",
                                "metadata": {"host": "github.com"},
                                "chains": ["旧节点", "🔰 节点选择"],
                            },
                        ]
                    }
                calls.append((method, path))
                return {}

        closed = Controller().close_stale_ai_connections(
            "新节点", load_settings()["ai_domain_suffixes"]
        )
        self.assertEqual(closed, 2)
        self.assertEqual(
            calls,
            [
                ("DELETE", "/connections/old-ai"),
                ("DELETE", "/connections/older-ai"),
            ],
        )


class PoolTests(unittest.TestCase):
    def make_entry(self, ip, asn, country, successes=10, latency=300):
        entry = WATCHDOG.node_template("ss")
        entry.update(
            {
                "exit_ip": ip,
                "asn": asn,
                "exit_country": country,
                "openai_status": "healthy",
                "candidate_eligible": True,
                "candidate_verified_at": 10_000,
                "deep_verified_at": 10_000,
                "successes": successes,
                "median_ms": latency,
                "last_success_at": 10_000,
            }
        )
        return entry

    def test_pool_deduplicates_shared_exit(self):
        config = load_settings()
        config["active_pool_max"] = 3
        config["warm_pool_max"] = 3
        config["active_candidate_ttl_seconds"] = 2_000
        config["warm_candidate_ttl_seconds"] = 2_000
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "节点A": self.make_entry("192.0.2.1", "AS1", "US", 20, 200),
            "节点A重复": self.make_entry("192.0.2.1", "AS1", "US", 2, 100),
            "节点B": self.make_entry("198.51.100.2", "AS2", "JP", 10, 300),
            "节点C": self.make_entry("203.0.113.3", "AS3", "SG", 10, 350),
        }
        catalog = {name: "ss" for name in state["nodes"]}
        WATCHDOG.rebuild_pools(state, catalog, config, 11_000)
        combined = state["pools"]["active"] + state["pools"]["warm"]
        self.assertEqual(len(combined), 3)
        self.assertIn("节点A", combined)
        self.assertNotIn("节点A重复", combined)
        self.assertEqual(state["pools"]["independent_exit_count"], 3)
        self.assertEqual(state["pools"]["duplicate_exit_groups"], 1)

    def test_current_healthy_node_is_retained(self):
        config = load_settings()
        config["active_pool_max"] = 2
        config["active_candidate_ttl_seconds"] = 2_000
        config["warm_candidate_ttl_seconds"] = 2_000
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "稳定节点": self.make_entry("192.0.2.1", "AS1", "US", 100, 100),
            "当前节点": self.make_entry("198.51.100.2", "AS2", "JP", 2, 800),
            "第三节点": self.make_entry("203.0.113.3", "AS3", "SG", 50, 120),
        }
        catalog = {name: "ss" for name in state["nodes"]}
        WATCHDOG.rebuild_pools(state, catalog, config, 11_000, current="当前节点")
        self.assertIn("当前节点", state["pools"]["active"])

    def test_candidate_order_prefers_different_exit_and_asn(self):
        state = WATCHDOG.default_state()
        for name, ip, asn in [
            ("当前", "192.0.2.1", "AS1"),
            ("同出口", "192.0.2.1", "AS1"),
            ("同ASN", "198.51.100.2", "AS1"),
            ("独立出口", "203.0.113.3", "AS3"),
        ]:
            entry = self.make_entry(ip, asn, "US")
            entry["preflight_ok"] = True
            state["nodes"][name] = entry
        ordered = WATCHDOG.candidate_order(state, ["同出口", "同ASN", "独立出口"], "当前")
        self.assertEqual(ordered[0], "独立出口")

    def test_candidate_order_deduplicates_shared_exit(self):
        state = WATCHDOG.default_state()
        for name, ip, asn, successes in [
            ("当前", "192.0.2.1", "AS1", 100),
            ("出口A优选", "198.51.100.2", "AS2", 50),
            ("出口A重复", "198.51.100.2", "AS2", 10),
            ("出口B", "203.0.113.3", "AS3", 20),
        ]:
            entry = self.make_entry(ip, asn, "US", successes)
            entry["preflight_ok"] = True
            state["nodes"][name] = entry
        ordered = WATCHDOG.candidate_order(
            state,
            ["出口A优选", "出口A重复", "出口B"],
            "当前",
        )
        self.assertEqual(ordered, ["出口A优选", "出口B"])

    def test_quarantine_requires_recovery(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        duration = WATCHDOG.quarantine_node(state, "节点", config, 1_000)
        entry = state["nodes"]["节点"]
        self.assertEqual(duration, 300)
        self.assertTrue(entry["needs_recovery"])
        for offset in range(3):
            WATCHDOG.apply_deep_scan(
                state,
                {
                    "node": "节点",
                    "route": healthy_route(),
                    "geo": {
                        "ok": True,
                        "exit_ip": "192.0.2.10",
                        "exit_country": "US",
                        "exit_region": "US",
                        "asn": "AS1",
                        "as_organization": "Example",
                    },
                },
                1_301 + offset * 6,
                config,
            )
        self.assertFalse(entry["needs_recovery"])

    def test_shallow_preflight_cannot_clear_recovery(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        WATCHDOG.quarantine_node(state, "节点", config, 1_000)
        for offset in range(5):
            WATCHDOG.record_preflight(
                state,
                "节点",
                True,
                100,
                1_301 + offset,
                config,
            )
        self.assertTrue(state["nodes"]["节点"]["needs_recovery"])
        self.assertEqual(state["nodes"]["节点"]["recovery_successes"], 0)

    def test_candidate_samples_require_spacing_and_freshness(self):
        config = load_settings()
        legacy = WATCHDOG.node_template("ss")
        legacy["candidate_samples"] = [
            {"time": 100, "eligible": True},
            {"time": 106, "eligible": True},
        ]
        self.assertFalse(WATCHDOG.candidate_has_required_samples(legacy, config, 106))

        entry = WATCHDOG.node_template("ss")
        entry["candidate_samples"] = [
            {"time": 100, "eligible": True, "retry_free": True},
            {"time": 101, "eligible": True, "retry_free": True},
        ]
        self.assertFalse(WATCHDOG.candidate_has_required_samples(entry, config, 101))
        entry["candidate_samples"][-1]["time"] = 106
        self.assertTrue(WATCHDOG.candidate_has_required_samples(entry, config, 106))
        self.assertFalse(WATCHDOG.candidate_has_required_samples(entry, config, 200))

        assisted = WATCHDOG.node_template("ss")
        assisted["candidate_samples"] = [
            {"time": 100, "eligible": True, "retry_free": False},
            {"time": 106, "eligible": True, "retry_free": False},
        ]
        self.assertFalse(WATCHDOG.candidate_has_required_samples(assisted, config, 106))
        assisted["candidate_samples"][-1]["retry_free"] = True
        self.assertTrue(WATCHDOG.candidate_has_required_samples(assisted, config, 106))

    def test_retry_assisted_deep_sample_is_usable_but_not_retry_free(self):
        state = WATCHDOG.default_state()
        result = healthy_route()
        result["recovered_hard_targets"] = ["openai_api"]
        WATCHDOG.update_route_observation(
            state,
            "候选",
            result,
            1_000,
            deep=True,
            source="candidate_preflight",
        )
        entry = state["nodes"]["候选"]
        self.assertTrue(entry["candidate_eligible"])
        self.assertTrue(entry["candidate_samples"][-1]["eligible"])
        self.assertFalse(entry["candidate_samples"][-1]["retry_free"])

    def test_web_feedback_applies_to_shared_exit_and_expires(self):
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "节点A": self.make_entry("192.0.2.1", "AS1", "US"),
            "节点A重复": self.make_entry("192.0.2.1", "as1", "us"),
            "节点B": self.make_entry("198.51.100.2", "AS2", "JP"),
        }
        result = WATCHDOG.record_web_feedback(
            state,
            "节点A",
            WATCHDOG.WEB_FEEDBACK_REJECTED,
            1_000,
            100,
            "browser_login_failed",
        )
        self.assertEqual(set(result["affected_nodes"]), {"节点A", "节点A重复"})
        self.assertEqual(
            WATCHDOG.web_feedback_status(state["nodes"]["节点A重复"], 1_050),
            WATCHDOG.WEB_FEEDBACK_REJECTED,
        )
        self.assertEqual(
            WATCHDOG.web_feedback_status(state["nodes"]["节点B"], 1_050),
            "unknown",
        )
        self.assertEqual(
            WATCHDOG.web_feedback_status(state["nodes"]["节点A"], 1_100),
            "unknown",
        )

    def test_web_feedback_invalidates_when_exit_fingerprint_changes(self):
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "动态出口": self.make_entry("192.0.2.1", "AS1", "US"),
        }
        WATCHDOG.record_web_feedback(
            state,
            "动态出口",
            WATCHDOG.WEB_FEEDBACK_REJECTED,
            1_000,
            10_000,
            "browser_login_failed",
        )
        state["nodes"]["动态出口"]["exit_ip"] = "198.51.100.2"
        self.assertEqual(
            WATCHDOG.web_feedback_status(state["nodes"]["动态出口"], 1_050),
            "unknown",
        )

    def test_rejected_exit_is_not_a_pool_or_failover_candidate(self):
        config = load_settings()
        config["deep_verification_ttl_seconds"] = 20_000
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.make_entry("192.0.2.1", "AS1", "US"),
            "网页失败": self.make_entry("198.51.100.2", "AS2", "JP"),
            "未知": self.make_entry("203.0.113.3", "AS3", "SG"),
        }
        WATCHDOG.record_web_feedback(
            state,
            "网页失败",
            WATCHDOG.WEB_FEEDBACK_REJECTED,
            10_500,
            1_000,
            "browser_login_failed",
        )
        catalog = {name: "ss" for name in state["nodes"]}
        WATCHDOG.rebuild_pools(state, catalog, config, 11_000, current="当前")
        self.assertNotIn("网页失败", state["pools"]["active"])
        self.assertNotIn("网页失败", state["pools"]["warm"])
        ordered = WATCHDOG.candidate_order(
            state,
            ["网页失败", "未知"],
            "当前",
            11_000,
        )
        self.assertEqual(ordered, ["未知"])

    def test_confirmed_browser_exit_is_preferred_over_unknown(self):
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.make_entry("192.0.2.1", "AS1", "US"),
            "未知": self.make_entry("198.51.100.2", "AS2", "JP"),
            "已确认": self.make_entry("203.0.113.3", "AS3", "SG"),
        }
        for name in ("未知", "已确认"):
            state["nodes"][name]["preflight_ok"] = True
        WATCHDOG.record_web_feedback(
            state,
            "已确认",
            WATCHDOG.WEB_FEEDBACK_CONFIRMED,
            1_000,
            10_000,
            "browser_login_success",
        )
        ordered = WATCHDOG.candidate_order(
            state,
            ["未知", "已确认"],
            "当前",
            1_050,
        )
        self.assertEqual(ordered[0], "已确认")

    def test_confirmed_browser_feedback_does_not_bypass_network_quarantine(self):
        config = load_settings()
        config["deep_verification_ttl_seconds"] = 20_000
        state = WATCHDOG.default_state()
        entry = self.make_entry("203.0.113.3", "AS3", "SG")
        entry["cooldown_until"] = 2_000
        entry["needs_recovery"] = True
        state["nodes"] = {"冷却节点": entry}
        WATCHDOG.record_web_feedback(
            state,
            "冷却节点",
            WATCHDOG.WEB_FEEDBACK_CONFIRMED,
            1_000,
            10_000,
            "browser_login_success",
        )
        self.assertFalse(WATCHDOG.pool_eligible(entry, config, 1_050))


class SwitchingGuardTests(unittest.TestCase):
    @staticmethod
    def switch_entry(ip, asn, successes):
        entry = WATCHDOG.node_template("ss")
        entry.update(
            {
                "exit_ip": ip,
                "exit_country": "US",
                "asn": asn,
                "openai_status": "healthy",
                "candidate_eligible": True,
                "candidate_verified_at": 10_000,
                "deep_verified_at": 10_000,
                "successes": successes,
                "last_success_at": 10_000,
            }
        )
        return entry

    def test_all_unavailable_notifies_once_per_episode(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        self.assertTrue(WATCHDOG.mark_all_unavailable(state, config, 1_000))
        self.assertFalse(WATCHDOG.mark_all_unavailable(state, config, 1_010))
        WATCHDOG.clear_all_unavailable(state)
        self.assertTrue(WATCHDOG.mark_all_unavailable(state, config, 2_000))

    def test_prepare_candidate_requires_two_isolated_samples(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "当前"
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 20),
            "候选": WATCHDOG.node_template("ss"),
        }
        state["nodes"]["候选"].update(
            {"exit_ip": "198.51.100.2", "asn": "AS2", "exit_country": "JP"}
        )
        catalog = {"当前": "ss", "候选": "ss"}
        timestamp = WATCHDOG.now_ts()
        scan_result = {
            "node": "候选",
            "route": healthy_route(),
            "geo": {
                "ok": True,
                "exit_ip": "198.51.100.2",
                "exit_country": "JP",
                "exit_region": "JP",
                "asn": "AS2",
                "as_organization": "Example",
            },
        }
        WATCHDOG.ensure_failover_episode(state, "当前", timestamp)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                WATCHDOG,
                "preflight_nodes",
                return_value=[{"node": "候选", "ok": True, "latency_ms": 100}],
            ),
            mock.patch.object(
                WATCHDOG,
                "_isolated_candidate_rounds",
                return_value={
                    "node": "候选",
                    "observations": [
                        {"time": timestamp - 6, "result": scan_result},
                        {"time": timestamp, "result": scan_result},
                    ],
                    "error": None,
                },
            ),
        ):
            prepared = WATCHDOG.prepare_failover_candidates(
                FakeController("当前"),
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
            )
        self.assertEqual([item["node"] for item in prepared], ["候选"])
        self.assertTrue(
            WATCHDOG.candidate_has_required_samples(state["nodes"]["候选"], config, timestamp)
        )

    def test_v2_state_migration_preserves_evidence_but_requires_revalidation(self):
        old = WATCHDOG.default_state()
        old["schema_version"] = 2
        entry = self.switch_entry("192.0.2.10", "AS1", 20)
        entry["openai_status"] = "unavailable"
        entry["web_feedback"] = {
            "status": "confirmed",
            "observed_at": 1_000,
            "expires_at": 9_999_999_999,
            "exit_fingerprint": {
                "exit_ip": "192.0.2.10",
                "asn": "AS1",
                "exit_country": "US",
            },
            "reason": "browser_login_success",
        }
        old["nodes"] = {"节点": entry}
        old["switch_history"] = [{"old_node": "A", "new_node": "节点"}]
        migrated = WATCHDOG.migrate_v2_state(old)
        migrated_entry = migrated["nodes"]["节点"]
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated_entry["exit_ip"], "192.0.2.10")
        self.assertEqual(migrated_entry["web_feedback"]["status"], "confirmed")
        self.assertEqual(migrated_entry["openai_status"], "unknown")
        self.assertFalse(migrated_entry["candidate_eligible"])
        self.assertEqual(migrated["pools"]["cold"], ["节点"])
        self.assertEqual(len(migrated["switch_history"]), 1)

    def test_manual_selection_does_not_create_switch_history(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "节点A"
        catalog = {"节点A": "ss", "节点B": "ss"}
        fake = FakeController("节点B")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "monitor.jsonl"
            with mock.patch.object(WATCHDOG, "route_probe", return_value=healthy_route()):
                result = WATCHDOG.health_iteration(
                    fake,
                    state,
                    catalog,
                    config,
                    state_path,
                    log_path,
                )
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(state["switch_history"], [])
        self.assertEqual(fake.selections, [])

    def test_healthy_node_never_switches_for_latency(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "节点B"
        catalog = {"节点A": "ss", "节点B": "ss"}
        fake = FakeController("节点B")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "monitor.jsonl"
            with mock.patch.object(WATCHDOG, "route_probe", return_value=healthy_route(5_000)):
                WATCHDOG.health_iteration(
                    fake,
                    state,
                    catalog,
                    config,
                    state_path,
                    log_path,
                )
        self.assertEqual(fake.selections, [])

    def test_timing_budget_is_within_thirty_seconds(self):
        config = load_settings()
        candidate_commit_budget = (
            int(config["candidate_commit_preflight_timeout_ms"]) / 1000
            + int(config["switch_connection_wait_seconds"])
            + max(int(item["timeout_seconds"]) for item in config["active_probes"])
        )
        first_candidate_upper_bound = (
            int(config["failure_confirmation_min_gap_seconds"]) + candidate_commit_budget
        )
        second_candidate_upper_bound = first_candidate_upper_bound + candidate_commit_budget
        self.assertEqual(config["failure_rounds_before_switch"], 2)
        self.assertEqual(config["candidate_prepare_count"], 3)
        self.assertLessEqual(first_candidate_upper_bound, 20)
        self.assertLessEqual(second_candidate_upper_bound, 30)

    def test_different_targets_form_two_consecutive_hard_rounds(self):
        config = load_settings()
        config["failure_confirmation_min_gap_seconds"] = 0
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "节点B"
        catalog = {"节点A": "ss", "节点B": "ss"}
        fake = FakeController("节点B")
        api_failure = {
            "classification": "hard_failure",
            "usable": False,
            "hard_reasons": ["openai_api:timeout"],
            "soft_reasons": [],
            "median_ms": None,
            "probes": {},
        }
        auth_failure = {
            "classification": "hard_failure",
            "usable": False,
            "hard_reasons": ["openai_auth:timeout"],
            "soft_reasons": [],
            "median_ms": None,
            "probes": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "monitor.jsonl"
            with (
                mock.patch.object(
                    WATCHDOG,
                    "route_probe",
                    side_effect=[api_failure, auth_failure],
                ),
                mock.patch.object(
                    WATCHDOG,
                    "direct_network_probe",
                    return_value={"ok": True, "results": {}},
                ),
                mock.patch.object(
                    WATCHDOG,
                    "prepare_failover_candidates",
                    return_value=[],
                ),
                mock.patch.object(
                    WATCHDOG,
                    "failover",
                    return_value={
                        "ok": False,
                        "reason": "candidates_not_ready",
                    },
                ) as failover,
            ):
                result = WATCHDOG.health_iteration(
                    fake, state, catalog, config, state_path, log_path
                )
        self.assertEqual(result["status"], "candidate_retry_backoff")
        self.assertEqual(state["monitor"]["consecutive_hard_failures"], 2)
        failover.assert_called_once()
        self.assertEqual(fake.selections, [])

    def test_same_target_switches_after_second_hard_failure(self):
        config = load_settings()
        config.update(
            {
                "active_pool_max": 2,
                "warm_pool_max": 0,
                "failure_confirmation_min_gap_seconds": 0,
            }
        )
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "节点B"
        state["nodes"] = {
            "节点A": self.switch_entry("198.51.100.2", "AS2", 50),
            "节点B": self.switch_entry("192.0.2.1", "AS1", 100),
        }
        catalog = {"节点A": "ss", "节点B": "ss"}
        hard_failure = {
            "classification": "hard_failure",
            "usable": False,
            "candidate_eligible": False,
            "hard_reasons": ["openai_api:timeout"],
            "soft_reasons": [],
            "median_ms": None,
            "probes": {},
        }

        class Controller(FakeController):
            def delay(self, node, url, timeout_ms, expected):
                del url, timeout_ms, expected
                return 120 if node == "节点A" else None

            def close_old_ai_connections(self, old_node, suffixes):
                del old_node, suffixes
                return 1

        controller = Controller("节点B")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                WATCHDOG,
                "route_probe",
                side_effect=[hard_failure, hard_failure, healthy_route()],
            ),
            mock.patch.object(
                WATCHDOG,
                "direct_network_probe",
                return_value={"ok": True, "results": {}},
            ),
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify"),
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {
                        "node": "节点A",
                        "layer": "active",
                        "verified_at": 10_000,
                        "classification": "healthy",
                    }
                ],
            ),
        ):
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "monitor.jsonl"
            result = WATCHDOG.health_iteration(
                controller,
                state,
                catalog,
                config,
                state_path,
                log_path,
            )

        self.assertEqual(result["status"], "switched")
        self.assertEqual(controller.current, "节点A")
        self.assertEqual(len(controller.selections), 1)

    def test_candidate_preparation_overlaps_confirmation_and_recovery_cancels_episode(self):
        config = load_settings()
        config["failure_confirmation_min_gap_seconds"] = 0
        state = WATCHDOG.default_state()
        state["monitor"]["last_seen_node"] = "节点B"
        catalog = {"节点A": "ss", "节点B": "ss"}
        hard_failure = {
            "classification": "hard_failure",
            "usable": False,
            "candidate_eligible": False,
            "hard_reasons": ["openai_api:timeout"],
            "soft_reasons": [],
            "median_ms": None,
            "probes": {},
        }
        preparation_started = threading.Event()
        release_preparation = threading.Event()

        def prepare(*args):
            del args
            preparation_started.set()
            self.assertTrue(release_preparation.wait(1))
            return []

        def confirmation_sleep(seconds):
            del seconds
            self.assertTrue(preparation_started.wait(1))
            release_preparation.set()

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                WATCHDOG,
                "route_probe",
                side_effect=[hard_failure, healthy_route()],
            ),
            mock.patch.object(
                WATCHDOG,
                "direct_network_probe",
                return_value={"ok": True, "results": {}},
            ),
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                side_effect=prepare,
            ),
            mock.patch.object(WATCHDOG.time, "sleep", side_effect=confirmation_sleep),
        ):
            result = WATCHDOG.health_iteration(
                FakeController("节点B"),
                state,
                catalog,
                config,
                Path(directory) / "state.json",
                Path(directory) / "monitor.jsonl",
            )
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(state["monitor"]["consecutive_hard_failures"], 0)
        self.assertIsNone(state["monitor"]["failover_episode"])

    def test_commit_preflight_failure_never_selects_candidate(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 20),
            "候选": self.switch_entry("198.51.100.2", "AS2", 10),
        }
        catalog = {"当前": "ss", "候选": "ss"}
        controller = FakeController("当前")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {
                        "node": "候选",
                        "layer": "active",
                        "verified_at": 10_000,
                        "classification": "healthy",
                    }
                ],
            ),
            mock.patch.object(WATCHDOG, "notify"),
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(controller.selections, [])
        self.assertTrue(state["nodes"]["候选"]["needs_recovery"])

    def test_commit_preflight_failure_does_not_consume_live_attempt_budget(self):
        config = load_settings()
        config["max_candidate_attempts_per_failover"] = 1
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 20),
            "陈旧候选": self.switch_entry("198.51.100.2", "AS2", 10),
            "健康候选": self.switch_entry("203.0.113.3", "AS3", 9),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def delay(self, node, url, timeout_ms, expected):
                del url, timeout_ms, expected
                return None if node == "陈旧候选" else 100

        controller = Controller("当前")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "route_probe", return_value=healthy_route()),
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify"),
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {"node": "陈旧候选", "layer": "active"},
                    {"node": "健康候选", "layer": "active"},
                ],
            ),
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(controller.current, "健康候选")
        self.assertEqual(controller.selections, [(config["group_name"], "健康候选")])

    def test_live_verification_rejects_candidate_when_followup_needs_retry(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 20),
            "候选": self.switch_entry("198.51.100.2", "AS2", 10),
            "尚未尝试": self.switch_entry("203.0.113.3", "AS3", 5),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return 100

        controller = Controller("当前")
        unstable = healthy_route()
        unstable["recovered_hard_targets"] = ["openai_api"]
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "route_probe", return_value=unstable) as route_probe,
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify") as notify,
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {
                        "node": "候选",
                        "layer": "active",
                        "verified_at": 10_000,
                        "classification": "healthy",
                    }
                ],
            ),
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(controller.current, "当前")
        self.assertEqual(
            controller.selections,
            [(config["group_name"], "候选"), (config["group_name"], "当前")],
        )
        self.assertTrue(state["nodes"]["候选"]["needs_recovery"])
        self.assertEqual(route_probe.call_count, 2)
        notify.assert_not_called()

    def test_retry_assisted_live_verification_requires_clean_followup(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 20),
            "候选": self.switch_entry("198.51.100.2", "AS2", 10),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return 100

        assisted = healthy_route()
        assisted["recovered_hard_targets"] = ["openai_api"]
        controller = Controller("当前")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                WATCHDOG,
                "route_probe",
                side_effect=[assisted, healthy_route()],
            ) as route_probe,
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify") as notify,
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[{"node": "候选", "layer": "active"}],
            ),
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(controller.current, "候选")
        self.assertEqual(route_probe.call_count, 2)
        notify.assert_called_once()

    def test_failover_uses_warm_pool_when_active_pool_fails(self):
        config = load_settings()
        config.update(
            {
                "active_pool_max": 2,
                "warm_pool_max": 1,
                "deep_verification_ttl_seconds": 10_000_000_000,
            }
        )
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 200),
            "活跃故障": self.switch_entry("198.51.100.2", "AS2", 100),
            "温备健康": self.switch_entry("203.0.113.3", "AS3", 10),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def __init__(self):
                super().__init__("当前")
                self.stale_close_calls = 0

            def delay(self, node, url, timeout_ms, expected):
                del url, timeout_ms, expected
                return 180 if node == "温备健康" else None

            def close_old_ai_connections(self, old_node, suffixes):
                del old_node, suffixes
                return 2

            def close_stale_ai_connections(self, new_node, suffixes):
                del new_node, suffixes
                return 2

        controller = Controller()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "route_probe", return_value=healthy_route()),
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify"),
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {
                        "node": "温备健康",
                        "layer": "warm",
                        "verified_at": 10_000,
                        "classification": "healthy",
                    }
                ],
            ) as prepare,
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["layer"], "warm")
        self.assertEqual(result["new_node"], "温备健康")
        self.assertEqual(prepare.call_args.kwargs["desired_count"], 2)

    def test_failover_notifies_all_unavailable_only_once(self):
        config = load_settings()
        config.update(
            {
                "active_pool_max": 2,
                "warm_pool_max": 1,
                "deep_verification_ttl_seconds": 10_000_000_000,
            }
        )
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 200),
            "活跃故障": self.switch_entry("198.51.100.2", "AS2", 100),
            "温备故障": self.switch_entry("203.0.113.3", "AS3", 50),
            "冷备故障": self.switch_entry("192.0.2.4", "AS4", 10),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def __init__(self):
                super().__init__("当前")
                self.stale_close_calls = 0

            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return None

        controller = Controller()
        WATCHDOG.ensure_failover_episode(state, "当前", 1_000)
        for node in ("活跃故障", "温备故障", "冷备故障"):
            WATCHDOG.record_episode_attempt(state, node)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "notify") as notify,
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[],
            ),
        ):
            log_path = Path(directory) / "monitor.jsonl"
            first = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                log_path,
                "当前",
                "测试故障",
            )
            second = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                log_path,
                "当前",
                "测试故障",
            )
        self.assertFalse(first["ok"])
        self.assertTrue(first["notified"])
        self.assertFalse(second["notified"])
        notify.assert_called_once_with("AI 代理监控", "当前机场全部不可用")

    def test_attempt_budget_does_not_report_all_unavailable(self):
        config = load_settings()
        config.update(
            {
                "active_pool_max": 3,
                "warm_pool_max": 0,
                "max_candidate_attempts_per_failover": 1,
                "deep_verification_ttl_seconds": 10_000_000_000,
            }
        )
        state = WATCHDOG.default_state()
        state["nodes"] = {
            "当前": self.switch_entry("192.0.2.1", "AS1", 200),
            "候选A": self.switch_entry("198.51.100.2", "AS2", 100),
            "候选B": self.switch_entry("203.0.113.3", "AS3", 50),
        }
        catalog = {name: "ss" for name in state["nodes"]}

        class Controller(FakeController):
            def __init__(self):
                super().__init__("当前")
                self.stale_close_calls = 0

            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return 180

            def close_old_ai_connections(self, old_node, suffixes):
                del old_node, suffixes
                return 0

            def close_stale_ai_connections(self, new_node, suffixes):
                del new_node, suffixes
                self.stale_close_calls += 1
                return 0

        hard_failure = {
            "classification": "hard_failure",
            "usable": False,
            "hard_reasons": ["openai_auth:timeout"],
            "soft_reasons": [],
            "median_ms": None,
            "probes": {},
        }
        controller = Controller()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "route_probe", return_value=hard_failure),
            mock.patch.object(WATCHDOG.time, "sleep"),
            mock.patch.object(WATCHDOG, "notify") as notify,
            mock.patch.object(
                WATCHDOG,
                "prepare_failover_candidates",
                return_value=[
                    {
                        "node": "候选A",
                        "layer": "active",
                        "verified_at": 10_000,
                        "classification": "healthy",
                    }
                ],
            ),
        ):
            result = WATCHDOG.failover(
                controller,
                state,
                catalog,
                config,
                Path(directory) / "monitor.jsonl",
                "当前",
                "测试故障",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "candidates_not_ready")
        self.assertFalse(result["notified"])
        self.assertEqual(len(result["attempted"]), 1)
        self.assertEqual(controller.current, "当前")
        self.assertEqual(controller.stale_close_calls, 0)
        notify.assert_not_called()


class CatalogTests(unittest.TestCase):
    def test_probe_stack_change_invalidates_old_candidate_evidence(self):
        config = load_settings()
        state = WATCHDOG.default_state()
        entry = WATCHDOG.node_template("ss")
        entry.update(
            {
                "candidate_eligible": True,
                "candidate_verified_at": 1_000,
                "candidate_samples": [{"time": 1_000, "eligible": True}],
            }
        )
        state["nodes"] = {"节点": entry}
        state["inventory"]["probe_stack_signature"] = "old"
        with (
            mock.patch.object(WATCHDOG, "load_real_nodes", return_value={"节点": "ss"}),
            mock.patch.object(
                WATCHDOG,
                "runtime_probe_stack_signature",
                return_value="new",
            ),
        ):
            WATCHDOG.refresh_catalog(state, config, 2_000)
        self.assertFalse(entry["candidate_eligible"])
        self.assertEqual(entry["candidate_verified_at"], 0)
        self.assertEqual(entry["candidate_samples"], [])

    def test_notice_and_groups_are_not_nodes(self):
        exclude = re.compile(load_settings()["node_exclude_regex"], re.I)
        self.assertTrue(
            WATCHDOG.is_real_proxy(
                {"name": "日本01", "type": "ss", "server": "x", "port": 443},
                exclude,
            )
        )
        self.assertFalse(
            WATCHDOG.is_real_proxy(
                {
                    "name": "剩余流量 10GB",
                    "type": "ss",
                    "server": "x",
                    "port": 443,
                },
                exclude,
            )
        )
        self.assertFalse(WATCHDOG.is_real_proxy({"name": "自动选择", "type": "Selector"}, exclude))


if __name__ == "__main__":
    unittest.main(verbosity=2)
