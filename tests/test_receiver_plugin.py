import json
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import helpers
from helpers import FakeInjector, delivery, event  # noqa: F401

from paseo_bridge import plugin, signature
from paseo_bridge.dispatcher import Dispatcher
from paseo_bridge.labels import SessionRef
from paseo_bridge.receiver import Receiver
from paseo_bridge.store import Store

SECRET = "receiver-secret-0123456789"


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.s = helpers.settings(listen_port=0, coalesce_seconds=0)
        self.store = Store(":memory:")
        self.inject = FakeInjector()
        self.d = Dispatcher(self.s, self.store, self.inject, own_profile="default")
        self.r = Receiver(self.s, self.d, lambda: SECRET, lambda: {})
        self.r.start()
        host, port = self.r.address
        self.url = f"http://{host}:{port}"

    def tearDown(self):
        self.r.stop()
        self.store.close()

    def post(self, body: bytes, headers: dict, path="/paseo/events"):
        req = urllib.request.Request(self.url + path, data=body, headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def signed(self, payload: dict, msg_id="dlv-1", secret=SECRET):
        body = json.dumps(payload).encode()
        return body, signature.signing_headers("standard-webhooks", secret, msg_id, body)

    def test_accepts_signed_delivery_and_dedupes_retry(self):
        body, h = self.signed(delivery(event(1)))
        self.assertEqual(self.post(body, h)[0], 202)
        self.assertEqual(self.post(body, h), (200, {"ok": True, "duplicate": True, "queued": 0}))
        self.d.process_due()
        self.assertEqual(len(self.inject.calls), 1)

    def test_rejects_bad_signature_wrong_path_and_garbage(self):
        body, h = self.signed(delivery(event(1)), secret="another-secret-123456")
        self.assertEqual(self.post(body, h)[0], 401)
        body, h = self.signed(delivery(event(1)))
        self.assertEqual(self.post(body, h, path="/other")[0], 404)
        body = b"[1,2]"
        self.assertEqual(self.post(body, signature.signing_headers("standard-webhooks", SECRET, "x", body))[0], 400)
        self.d.process_due()
        self.assertEqual(self.inject.calls, [])

    def test_test_delivery_is_acknowledged_not_injected(self):
        body, h = self.signed({"type": "webhook.test", "event": {"type": "webhook.test", "agentId": "test"}})
        self.assertEqual(self.post(body, h), (200, {"ok": True, "test": True}))
        self.d.process_due()
        self.assertEqual(self.inject.calls, [])

    def test_healthz(self):
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["ok"], True)


class FakeManager:
    has_gateway_message_injector = True
    has_tui_message_injector = False

    def __init__(self, session_store=None):
        self._gateway_message_injector = (mock.Mock(session_store=session_store), None)


class FakeCtx:
    profile_name = "default"

    def __init__(self, config=None, accept=True, session_store=None):
        self.config = config or {}
        self.accept = accept
        self.hooks = {}
        self.commands = {}
        self.unload = []
        self.injected = []
        self._manager = FakeManager(session_store)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description=""):
        self.commands[name] = fn

    def on_unload(self, fn):
        self.unload.append(fn)

    def inject_message(self, content, role="user", *, session_key=None, origin=None):
        self.injected.append((session_key if origin is None else origin, content))
        return self.accept


class PollWiringTest(unittest.TestCase):
    def test_register_polls_paseo_mcp_and_injects(self):
        from fake_paseo_mcp import FakePaseoMcp
        fake = FakePaseoMcp("tok-1234567890")
        self.addCleanup(fake.close)
        ctx = FakeCtx(config={"paseo_mcp_url": fake.url, "poll_timeout_seconds": 1, "coalesce_seconds": 0})
        env = {"HERMES_PASEO_BRIDGE_PASEO_MCP_TOKEN": "tok-1234567890", "HERMES_PASEO_BRIDGE_SECRET": ""}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(plugin, "_data_dir", return_value=__import__("pathlib").Path(__import__("tempfile").mkdtemp())):
            plugin.register(ctx)
            self.addCleanup(lambda: [fn() for fn in ctx.unload])
            for _ in range(60):
                if fake.calls:
                    break
                time.sleep(0.05)
            fake.emit(event(0))
            for _ in range(100):
                if ctx.injected:
                    break
                time.sleep(0.05)
        self.assertEqual(ctx.injected[0][0], "agent:main:slack:dm:T1:D1:1.0")
        status = ctx.commands["paseo-bridge"]("")
        self.assertIn("long-poll only", status)
        self.assertIn("Long-poll: polling", status)


class PluginTest(unittest.TestCase):
    def bridge(self, **kw):
        ctx = FakeCtx(**kw)
        b = plugin.Bridge(ctx, helpers.settings(listener="never"), Store(":memory:"))
        self.addCleanup(b.store.close)
        return ctx, b

    def test_pre_tool_call_adds_labels_for_create_only(self):
        ctx, b = self.bridge()
        ref = SessionRef(session_key="agent:main:local:dm:local-1", profile="default", session_id="s1", platform="local")
        with mock.patch.object(plugin, "current_session", return_value=ref):
            out = b.on_pre_tool_call("mcp__paseo__paseo_create_agent", {"cwd": "/w", "prompt": "p", "labels": {"todo": "t1"}})
            self.assertEqual(out["action"], "modify")
            self.assertEqual(out["args"]["labels"]["hermes_session_key"], "agent:main:local:dm:local-1")
            self.assertEqual(out["args"]["labels"]["todo"], "t1")
            self.assertEqual(set(out["args"]), {"labels"})  # nothing else in the call changes
            self.assertIsNone(b.on_pre_tool_call("mcp__paseo__paseo_send_message", {"labels": {}}))
            self.assertIsNone(b.on_pre_tool_call("terminal", {"command": "ls"}))
        with mock.patch.object(plugin, "current_session", return_value=SessionRef(session_key="")):
            self.assertIsNone(b.on_pre_tool_call("mcp__paseo__paseo_create_agent", {}))

    def test_pre_tool_call_steer_records_route(self):
        ctx, b = self.bridge()
        ref = SessionRef(session_key="agent:main:local:dm:ui-2", profile="default", session_id="s2")
        with mock.patch.object(plugin, "current_session", return_value=ref):
            out = b.on_pre_tool_call("mcp__paseo__paseo_send_message", {"agent_id": "agent-7", "text": "go on"})
        self.assertIsNone(out)  # the call itself is not changed
        self.assertEqual(b.store.route("agent-7"), ("agent:main:local:dm:ui-2", "default"))
        with mock.patch.object(plugin, "current_session", return_value=SessionRef(session_key="")):
            b.on_pre_tool_call("mcp__paseo__paseo_send_message", {"agent_id": "agent-8", "text": "x"})
        self.assertIsNone(b.store.route("agent-8"))

    def test_home_channel_injection(self):
        import enum
        from types import SimpleNamespace

        class Platform(enum.Enum):
            TELEGRAM = "telegram"
            SLACK = "slack"

        ctx, b = self.bridge()
        runner = ctx._manager._gateway_message_injector[0]
        runner.config = SimpleNamespace(platforms={
            Platform.TELEGRAM: SimpleNamespace(enabled=True, home_channel=None),
            Platform.SLACK: SimpleNamespace(enabled=True, home_channel=SimpleNamespace(chat_id="C1", thread_id="9.1")),
        })
        self.assertEqual(b.inject_home("", "hello"), (True, "slack/C1"))
        self.assertEqual(ctx.injected[-1][0], {"platform": "slack", "chat_id": "C1", "thread_id": "9.1"})
        ok, why = b.inject_home("telegram", "hello")
        self.assertFalse(ok)
        self.assertIn("no home channel configured for telegram", why)
        ctx._manager.has_gateway_message_injector = False
        self.assertIsNone(b.inject_home("", "hello")[0])

    def test_current_session_reads_hermes_session_env(self):
        env = {"HERMES_SESSION_KEY": "agent:main:slack:dm:T:D:1", "HERMES_SESSION_ID": "sid",
               "HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_CHAT_ID": "D", "HERMES_SESSION_THREAD_ID": "1"}
        with mock.patch.dict("os.environ", env):
            ref = plugin.current_session("default")
        self.assertEqual(ref.session_key, "agent:main:slack:dm:T:D:1")
        self.assertEqual((ref.platform, ref.chat_id, ref.thread_id, ref.session_id), ("slack", "D", "1", "sid"))

    def test_inject_paths(self):
        store = mock.Mock()
        store.lookup_by_session_key.side_effect = lambda k: object() if k.endswith(":known") else None
        ctx, b = self.bridge(session_store=store)
        self.assertEqual(b.inject("agent:main:slack:dm:T:known", "hi"), (True, ""))
        ok, why = b.inject("agent:main:slack:dm:T:gone", "hi")
        self.assertFalse(ok)
        self.assertIn("no such session", why)
        self.assertEqual(b.inject("ses_tui_key", "hi"), (True, ""))  # TUI keys skip the store check
        ctx.accept = False
        self.assertFalse(b.inject("agent:main:slack:dm:T:known", "hi")[0])
        ctx._manager.has_gateway_message_injector = False
        self.assertIsNone(b.inject("agent:main:slack:dm:T:known", "hi")[0])
        self.assertEqual([k for k, _ in ctx.injected], ["agent:main:slack:dm:T:known", "ses_tui_key", "agent:main:slack:dm:T:known"])

    def test_host_ready_degrades_without_manager_flags(self):
        ctx, b = self.bridge()
        self.assertTrue(b.host_ready())
        ctx._manager.has_gateway_message_injector = False
        self.assertFalse(b.host_ready())
        ctx._manager = object()  # a Hermes without these internals: let inject_message decide
        self.assertTrue(b.host_ready())
        self.assertEqual(b.inject("agent:main:slack:dm:T:x", "hi"), (True, ""))

    def test_compaction_alias(self):
        ctx, b = self.bridge()
        b.on_post_compaction(session_id="new", old_session_id="old")
        self.assertEqual(b.store.resolve("old"), "new")

    def test_register_wires_everything_and_refuses_bad_settings(self):
        ctx = FakeCtx(config={"listener": "never"})
        with mock.patch.object(plugin, "_data_dir", return_value=__import__("pathlib").Path(__import__("tempfile").mkdtemp())):
            plugin.register(ctx)
        self.assertEqual(set(ctx.hooks), {"pre_tool_call", "post_compaction"})
        self.assertIn("paseo-bridge", ctx.commands)
        self.assertIn("Listener: off", ctx.commands["paseo-bridge"](""))
        for fn in ctx.unload:
            fn()
        bad = FakeCtx(config={"fallback": {"mode": "forward"}})
        with self.assertLogs("hermes_paseo_bridge", "ERROR"):
            plugin.register(bad)
        self.assertEqual(bad.hooks, {})

    def test_listener_waits_for_host_and_needs_secret(self):
        ctx = FakeCtx()
        ctx._manager.has_gateway_message_injector = False
        b = plugin.Bridge(ctx, helpers.settings(listen_port=0, secret_env="HPB_TEST_SECRET"), Store(":memory:"))
        with mock.patch.dict("os.environ", {"HPB_TEST_SECRET": SECRET}):
            b.start()
            time.sleep(0.3)
            self.assertEqual(b.listener_state, "waiting for the gateway")
            ctx._manager.has_gateway_message_injector = True
            for _ in range(30):
                if b.listener_state.startswith("listening"):
                    break
                time.sleep(0.1)
            self.assertTrue(b.listener_state.startswith("listening on http://127.0.0.1:"))
            b.stop()
        ctx2 = FakeCtx()
        b2 = plugin.Bridge(ctx2, helpers.settings(listen_port=0, secret_env="HPB_UNSET_SECRET"), Store(":memory:"))
        with self.assertLogs("hermes_paseo_bridge", "ERROR"):
            b2.start()
            time.sleep(0.3)
        self.assertIn("is not set", b2.listener_state)
        b2.stop()


if __name__ == "__main__":
    unittest.main()
