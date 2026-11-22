import unittest

import helpers

from paseo_bridge.config import SettingsError, load_settings, with_env
from paseo_bridge.labels import SessionRef, bridge_labels, tool_matches


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.s = helpers.settings()
        self.session = SessionRef(session_key="agent:main:slack:dm:T1:D1:17.5", profile="default",
                                  session_id="20261118_1", platform="slack", chat_id="D1", thread_id="17.5")

    def test_tool_globs(self):
        self.assertTrue(tool_matches("mcp__paseo__paseo_create_agent", self.s.tools))
        self.assertTrue(tool_matches("mcp__paseo_work__paseo_create_agent", self.s.tools))
        self.assertFalse(tool_matches("mcp__paseo__paseo_send_message", self.s.tools))
        self.assertFalse(tool_matches("paseo_create_agent", self.s.tools))

    def test_adds_session_labels_and_keeps_existing(self):
        labels = bridge_labels({"todo": "T-1", "card": "c9", "kind": "fix"}, self.session, self.s)
        self.assertEqual(labels["todo"], "T-1")
        self.assertEqual(labels["card"], "c9")
        self.assertEqual(labels["kind"], "fix")
        self.assertEqual(labels["hermes_session_key"], "agent:main:slack:dm:T1:D1:17.5")
        self.assertEqual(labels["hermes_profile"], "default")
        self.assertEqual(labels["hermes_session_id"], "20261118_1")
        self.assertEqual(labels["hermes_platform"], "slack")
        self.assertEqual(labels["origin_ref"], "20261118_1")
        self.assertEqual(labels["hermes_chat_id"], "D1")
        self.assertEqual(labels["hermes_thread_id"], "17.5")

    def test_own_labels_win_extras_fill_gaps(self):
        labels = bridge_labels({"hermes_session_key": "typed-by-model", "origin_ref": "kept"}, self.session, self.s)
        self.assertEqual(labels["hermes_session_key"], "agent:main:slack:dm:T1:D1:17.5")
        self.assertEqual(labels["origin_ref"], "kept")

    def test_no_session_no_labels(self):
        self.assertIsNone(bridge_labels({"todo": "x"}, SessionRef(session_key=""), self.s))

    def test_empty_values_are_left_out(self):
        labels = bridge_labels(None, SessionRef(session_key="ses_abc", profile="default"), self.s)
        self.assertEqual(labels, {"hermes_session_key": "ses_abc", "hermes_profile": "default"})

    def test_custom_prefix(self):
        s = helpers.settings(label_prefix="bot_", extra_labels={})
        labels = bridge_labels({}, self.session, s)
        self.assertIn("bot_session_key", labels)
        self.assertEqual(s.session_key_label, "bot_session_key")
        self.assertNotIn("origin_ref", labels)


class ConfigTest(unittest.TestCase):
    def test_defaults_from_empty_settings(self):
        s = load_settings(lambda k, d: d)
        self.assertEqual(s.listen_port, 8645)
        self.assertEqual(s.filter_labels, {"origin": "hermes"})
        self.assertEqual(s.fallback.mode, "home")
        self.assertEqual(s.event_types, ("agent.turn_ended", "agent.permission_requested", "agent.closed"))
        self.assertEqual(s.turn_outcomes, ("finished", "error", "limit"))
        self.assertEqual(s.steer_tools, ("mcp__*__paseo_send_message",))
        self.assertEqual(s.paseo_mcp_url, "")

    def test_overrides(self):
        cfg = {"listen_port": 9000, "event_types": ["agent.turn_ended"], "fallback": {"mode": "session", "session_key": "agent:main:slack:dm:T:D"}}
        s = load_settings(lambda k, d: cfg.get(k, d))
        self.assertEqual(s.listen_port, 9000)
        self.assertEqual(s.event_types, ("agent.turn_ended",))
        self.assertEqual(s.fallback.session_key, "agent:main:slack:dm:T:D")

    def test_invalid(self):
        for cfg in ({"fallback": {"mode": "forward"}}, {"fallback": {"mode": "session"}}, {"signature": "github"},
                    {"listen_port": "x"}, {"path": "no-slash"}, {"tools": [""]}, {"filter_labels": {"a": 1}},
                    {"max_turns_per_minute": 0}, {"listen_host": "0.0.0.0"}, {"listen_host": "192.168.1.5"},
                    {"paseo_mcp_url": "ftp://x"}, {"fallback": {"mode": "carrier-pigeon"}}):
            with self.subTest(cfg=cfg), self.assertRaises(SettingsError):
                load_settings(lambda k, d, cfg=cfg: cfg.get(k, d))

    def test_loopback_hosts_accepted(self):
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(load_settings(lambda k, d, h=host: h if k == "listen_host" else d).listen_host, host)

    def test_env_overrides_win_over_config(self):
        cfg = {"listen_port": 9000, "fallback": {"mode": "log"}, "tools": ["a"]}
        env = {"HERMES_PASEO_BRIDGE_LISTEN_PORT": "8700", "HERMES_PASEO_BRIDGE_PASEO_MCP_URL": "http://127.0.0.1:6768/mcp",
               "HERMES_PASEO_BRIDGE_FALLBACK_MODE": "session", "HERMES_PASEO_BRIDGE_FALLBACK_SESSION_KEY": "agent:main:x",
               "HERMES_PASEO_BRIDGE_FILTER_LABELS": '{"origin": "box"}', "HERMES_PASEO_BRIDGE_STEER_TOOLS": "x,y",
               "HERMES_PASEO_BRIDGE_COALESCE_SECONDS": "2.5", "HERMES_PASEO_BRIDGE_LISTEN_HOST": ""}
        s = load_settings(with_env(lambda k, d: cfg.get(k, d), env))
        self.assertEqual(s.listen_port, 8700)
        self.assertEqual(s.listen_host, "127.0.0.1")  # empty variable = not set
        self.assertEqual(s.paseo_mcp_url, "http://127.0.0.1:6768/mcp")
        self.assertEqual((s.fallback.mode, s.fallback.session_key), ("session", "agent:main:x"))
        self.assertEqual(s.filter_labels, {"origin": "box"})
        self.assertEqual(s.steer_tools, ("x", "y"))
        self.assertEqual(s.tools, ("a",))
        self.assertEqual(s.coalesce_seconds, 2.5)
        with self.assertRaises(SettingsError):
            load_settings(with_env(lambda k, d: d, {"HERMES_PASEO_BRIDGE_LISTEN_PORT": "many"}))

    def test_steer_tools_can_be_switched_off(self):
        self.assertEqual(load_settings(lambda k, d: [] if k == "steer_tools" else d).steer_tools, ())


if __name__ == "__main__":
    unittest.main()
