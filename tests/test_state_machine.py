import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"))

from pet.events import make_event  # noqa: E402
from pet.state_machine import ClockEvents, StateMachine  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
with open(os.path.join(ROOT, "config", "states.json"), encoding="utf-8") as fh:
    CONFIG = json.load(fh)


def ev(name, type_="touch"):
    return make_event(type_, name, source="test")


def run(fsm, seconds, dt=0.05):
    for _ in range(int(round(seconds / dt))):
        fsm.update(dt)


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.fsm = StateMachine(CONFIG, timer_overrides={"idle_timeout": 10})
        self.changes = []
        self.fsm.state_listeners.append(lambda o, n, e: self.changes.append((o, n)))

    def test_initial(self):
        self.assertEqual(self.fsm.state, "idle")
        self.assertEqual(self.fsm.animation(), "idle")
        self.assertEqual(self.fsm.eyes(), "normal")

    def test_pat_makes_happy_then_back_to_idle(self):
        self.assertEqual(self.fsm.handle_event(ev("pat")), "happy")
        self.assertEqual(self.fsm.eyes(), "happy")
        run(self.fsm, 2.9)
        self.assertEqual(self.fsm.state, "happy")
        run(self.fsm, 0.2)
        self.assertEqual(self.fsm.state, "idle")

    def test_idle_timeout_chain(self):
        run(self.fsm, 10.05)
        self.assertEqual(self.fsm.state, "bored")
        run(self.fsm, 10.05)
        self.assertEqual(self.fsm.state, "sleep")
        self.assertIn("zzz", self.fsm.effects())
        self.assertEqual(self.fsm.handle_event(ev("pat")), "idle")
        self.assertEqual(self.changes, [("idle", "bored"), ("bored", "sleep"), ("sleep", "idle")])

    def test_interaction_resets_idle_timer(self):
        run(self.fsm, 8)
        self.fsm.handle_event(ev("unknown_touch"))   # 沒有對應規則，但算互動
        run(self.fsm, 8)
        self.assertEqual(self.fsm.state, "idle")
        run(self.fsm, 2.1)
        self.assertEqual(self.fsm.state, "bored")

    def test_reset_input_timers_public_method(self):
        run(self.fsm, 8)
        self.fsm.reset_input_timers()
        run(self.fsm, 8)
        self.assertEqual(self.fsm.state, "idle")
        run(self.fsm, 2.1)
        self.assertEqual(self.fsm.state, "bored")

    def test_unknown_event_no_change(self):
        self.assertIsNone(self.fsm.handle_event(ev("whatever")))
        self.assertEqual(self.fsm.state, "idle")

    def test_wildcard_rules(self):
        self.fsm.handle_event(ev("sleep", "sim"))
        self.assertEqual(self.fsm.state, "sleep")
        self.fsm.handle_event(ev("wake", "sim"))
        self.assertEqual(self.fsm.state, "idle")

    def test_specific_rule_beats_wildcard(self):
        cfg = json.loads(json.dumps(CONFIG))
        cfg["transitions"].append({"from": "*", "event": "pat", "to": "sleep"})
        fsm = StateMachine(cfg)
        self.assertEqual(fsm.handle_event(ev("pat")), "happy")

    def test_stale_timer_event_ignored(self):
        self.fsm.handle_event(ev("pat"))           # happy
        stale = make_event("time", "idle_timeout", for_state="idle")
        self.assertIsNone(self.fsm.handle_event(stale))
        self.assertEqual(self.fsm.state, "happy")

    def test_timer_via_emit(self):
        out = []
        fsm = StateMachine(CONFIG, timer_overrides={"idle_timeout": 1}, emit=out.append)
        run(fsm, 1.05)
        self.assertEqual(fsm.state, "idle")          # 透過匯流排，還沒處理
        self.assertEqual([e["name"] for e in out], ["idle_timeout"])
        fsm.handle_event(out[0])
        self.assertEqual(fsm.state, "bored")

    def test_force_state(self):
        self.fsm.force("sleep")
        self.assertEqual(self.fsm.state, "sleep")
        with self.assertRaises(KeyError):
            self.fsm.force("nope")

    def test_invalid_config(self):
        bad = {"states": {"a": {}}, "transitions": [{"from": "a", "event": "x", "to": "b"}]}
        with self.assertRaises(ValueError):
            StateMachine(bad)


class GrowthTest(unittest.TestCase):
    def test_set_stage_hook(self):
        fsm = StateMachine(CONFIG)
        seen = []
        fsm.stage_listeners.append(lambda o, n: seen.append((o, n)))
        fsm.set_stage(2)
        self.assertEqual(fsm.growth.stage, 2)
        self.assertEqual(fsm.stage_name(), "長腳")
        self.assertEqual(seen, [(1, 2)])
        with self.assertRaises(KeyError):
            fsm.set_stage(99)

    def test_growth_value_does_not_auto_level(self):
        fsm = StateMachine(CONFIG)
        fsm.growth.add(1000, "test")
        self.assertEqual(fsm.growth.stage, 1)   # 推進規則尚未實作


class ClockTest(unittest.TestCase):
    def test_night_morning_edges(self):
        import datetime as dt
        hour = {"h": 22}
        out = []
        c = ClockEvents(out.append, 23, 7, now=lambda: dt.datetime(2026, 1, 1, hour["h"]))
        c.poll()
        hour["h"] = 23
        c.poll()
        c.poll()
        hour["h"] = 7
        c.poll()
        self.assertEqual([e["name"] for e in out], ["night", "morning"])


if __name__ == "__main__":
    unittest.main()
