import re
import tempfile
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


class PoolTests(unittest.TestCase):
    def make_entry(self, ip, asn, country, successes=10, latency=300):
        entry = WATCHDOG.node_template("ss")
        entry.update(
            {
                "exit_ip": ip,
                "asn": asn,
                "exit_country": country,
                "openai_status": "healthy",
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
        config["deep_verification_ttl_seconds"] = 20_000
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
        config["deep_verification_ttl_seconds"] = 20_000
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
            WATCHDOG.record_preflight(
                state,
                "节点",
                True,
                200,
                1_301 + offset,
                config,
            )
        self.assertFalse(entry["needs_recovery"])

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
        first_failure_to_switch_upper_bound = (
            int(config["monitor_interval_seconds"])
            + int(config["candidate_preflight_timeout_ms"]) // 1000
            + int(config["switch_connection_wait_seconds"])
            + max(int(item["timeout_seconds"]) for item in config["active_probes"])
        )
        self.assertEqual(config["failure_rounds_before_switch"], 2)
        self.assertGreaterEqual(config["candidate_concurrency"], config["active_pool_max"])
        self.assertLessEqual(first_failure_to_switch_upper_bound, 30)

    def test_different_targets_do_not_form_two_consecutive_failures(self):
        config = load_settings()
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
            ):
                first = WATCHDOG.health_iteration(
                    fake, state, catalog, config, state_path, log_path
                )
                second = WATCHDOG.health_iteration(
                    fake, state, catalog, config, state_path, log_path
                )
        self.assertEqual(first["status"], "waiting_confirmation")
        self.assertEqual(second["status"], "waiting_confirmation")
        self.assertEqual(state["monitor"]["consecutive_hard_failures"], 1)
        self.assertEqual(fake.selections, [])

    def test_same_target_switches_only_after_second_hard_failure(self):
        config = load_settings()
        config.update(
            {
                "active_pool_max": 2,
                "warm_pool_max": 0,
                "deep_verification_ttl_seconds": 10_000_000_000,
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
        ):
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "monitor.jsonl"
            first = WATCHDOG.health_iteration(
                controller,
                state,
                catalog,
                config,
                state_path,
                log_path,
            )
            self.assertEqual(controller.selections, [])
            second = WATCHDOG.health_iteration(
                controller,
                state,
                catalog,
                config,
                state_path,
                log_path,
            )

        self.assertEqual(first["status"], "waiting_confirmation")
        self.assertEqual(second["status"], "switched")
        self.assertEqual(controller.current, "节点A")
        self.assertEqual(len(controller.selections), 1)

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

            def delay(self, node, url, timeout_ms, expected):
                del url, timeout_ms, expected
                return 180 if node == "温备健康" else None

            def close_old_ai_connections(self, old_node, suffixes):
                del old_node, suffixes
                return 2

        controller = Controller()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "route_probe", return_value=healthy_route()),
            mock.patch.object(WATCHDOG.time, "sleep"),
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
        self.assertTrue(result["ok"])
        self.assertEqual(result["layer"], "warm")
        self.assertEqual(result["new_node"], "温备健康")

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

            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return None

        controller = Controller()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(WATCHDOG, "notify") as notify,
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

            def delay(self, node, url, timeout_ms, expected):
                del node, url, timeout_ms, expected
                return 180

            def close_old_ai_connections(self, old_node, suffixes):
                del old_node, suffixes
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
        self.assertEqual(result["reason"], "attempt_budget_exhausted")
        self.assertFalse(result["notified"])
        self.assertEqual(len(result["attempted"]), 1)
        self.assertEqual(controller.current, "当前")
        notify.assert_not_called()


class CatalogTests(unittest.TestCase):
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
