import unittest

import helpers
from paseo_bridge.config import SettingsError, load_settings
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
        self.assertEqual(s.fallback.mode, "log")

    def test_overrides(self):
        cfg = {"listen_port": 9000, "event_types": ["agent.turn_ended"], "fallback": {"mode": "session", "session_key": "agent:main:slack:dm:T:D"}}
        s = load_settings(lambda k, d: cfg.get(k, d))
        self.assertEqual(s.listen_port, 9000)
        self.assertEqual(s.event_types, ("agent.turn_ended",))
        self.assertEqual(s.fallback.session_key, "agent:main:slack:dm:T:D")

    def test_invalid(self):
        for cfg in ({"fallback": {"mode": "forward"}}, {"fallback": {"mode": "session"}}, {"signature": "github"},
                    {"listen_port": "x"}, {"path": "no-slash"}, {"tools": [""]}, {"filter_labels": {"a": 1}},
                    {"max_turns_per_minute": 0}):
            with self.subTest(cfg=cfg), self.assertRaises(SettingsError):
                load_settings(lambda k, d, cfg=cfg: cfg.get(k, d))


if __name__ == "__main__":
    unittest.main()
