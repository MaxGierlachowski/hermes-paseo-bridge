import os
import tempfile
import time
import unittest

import helpers
from fake_paseo_mcp import FakePaseoMcp
from helpers import Clock, FakeInjector, event

from paseo_bridge.dispatcher import Dispatcher
from paseo_bridge.poller import CURSOR_KEY, McpError, McpHttpClient, Poller
from paseo_bridge.store import INJECTED, Store

TOKEN = "read-token-123"


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePaseoMcp(TOKEN)
        self.addCleanup(self.fake.close)
        self.path = os.path.join(tempfile.mkdtemp(), "bridge.db")

    def make(self, store=None, token=TOKEN):
        self.clock = Clock()
        self.store = store or Store(self.path)
        self.addCleanup(self.store.close)
        self.inject = FakeInjector()
        self.d = Dispatcher(helpers.settings(coalesce_seconds=0), self.store, self.inject, own_profile="default", clock=self.clock)
        return Poller(McpHttpClient(self.fake.url, token), self.store, self.d.submit, timeout_seconds=1,
                      types=("agent.turn_ended", "agent.permission_requested", "agent.closed"))

    def test_first_call_takes_a_cursor_then_events_reach_the_session(self):
        p = self.make()
        self.fake.emit(event(0))  # before the bridge ever polled: not replayed
        self.assertEqual(p.poll_once(), 0)
        self.assertEqual(self.store.get_value(CURSOR_KEY), "ep1:1")
        self.fake.emit(event(0, type_="agent.turn_started"))
        self.fake.emit(event(0))
        self.assertEqual(p.poll_once(), 1)  # turn_started filtered by the server-side types
        self.assertEqual(self.store.get_value(CURSOR_KEY), "ep1:3")
        self.d.process_due()
        self.assertEqual(len(self.inject.calls), 1)
        self.assertEqual(self.fake.calls[-1]["arguments"]["types"], ["agent.turn_ended", "agent.permission_requested", "agent.closed"])

    def test_sse_answers(self):
        self.fake.sse = True
        p = self.make()
        p.poll_once()
        self.fake.emit(event(0))
        self.assertEqual(p.poll_once(), 1)

    def test_cursor_survives_a_restart_and_nothing_is_lost_or_repeated(self):
        p = self.make()
        p.poll_once()
        self.fake.emit(event(0, agent="a1"))
        p.poll_once()
        self.d.process_due()
        self.store.close()
        # Hermes is down; the agent finishes meanwhile.
        self.fake.emit(event(0, agent="a2"))
        store2 = Store(self.path)
        p2 = self.make(store=store2)
        self.assertEqual(p2.poll_once(), 1)
        self.d.process_due()
        self.assertEqual(len(self.inject.calls), 1)
        self.assertIn("a2", self.inject.calls[0][1])
        self.assertEqual(store2.status_of("evt:ep1:2")[0], INJECTED)

    def test_a_reread_range_is_not_injected_twice(self):
        p = self.make()
        p.poll_once()
        self.fake.emit(event(0))
        p.poll_once()
        self.store.set_value(CURSOR_KEY, "ep1:0")  # crash before the cursor was saved
        p.poll_once()
        self.d.process_due()
        self.assertEqual(len(self.inject.calls), 1)

    def test_gap_after_paseo_mcp_restart_is_logged_and_polling_continues(self):
        p = self.make()
        p.poll_once()
        self.fake.restart()
        with self.assertLogs("hermes_paseo_bridge", "WARNING") as logs:
            p.client.session_id = None
            p.poll_once()
        self.assertTrue(any("gap" in m and "agent-x" in m for m in logs.output))
        self.assertEqual(p.gaps, 1)
        self.assertEqual(self.store.get_value(CURSOR_KEY), "ep2:0")
        self.fake.emit(event(0))
        self.assertEqual(p.poll_once(), 1)

    def test_expired_session_is_reopened_by_the_loop(self):
        p = self.make()
        p.poll_once()
        self.fake.sessions.clear()
        self.fake.emit(event(0))
        p.start()
        self.addCleanup(p.stop)
        for _ in range(50):
            if p.polled:
                break
            time.sleep(0.05)
        self.assertEqual(p.polled, 1)
        self.assertTrue(p.state.startswith("polling"))

    def test_bad_token(self):
        p = self.make(token="wrong")
        with self.assertRaises(McpError) as ctx:
            p.poll_once()
        self.assertIn("401", str(ctx.exception))
        self.assertIsNone(self.store.get_value(CURSOR_KEY))

    def test_loop_waits_for_the_gateway(self):
        p = self.make()
        ready = []
        p.ready = lambda: bool(ready)
        p.start()
        self.addCleanup(p.stop)
        time.sleep(0.2)
        self.assertEqual(p.state, "waiting for the gateway")
        self.assertEqual(self.fake.calls, [])
        ready.append(1)
        for _ in range(50):
            if self.fake.calls:
                break
            time.sleep(0.05)
        self.assertTrue(self.fake.calls)


if __name__ == "__main__":
    unittest.main()
