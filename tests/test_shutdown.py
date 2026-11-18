import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "ups_final_shutdown", str(REPO / "scripts" / "ups-final-shutdown")
)
MODULE = importlib.util.module_from_spec(importlib.util.spec_from_loader(LOADER.name, LOADER))
sys.modules[LOADER.name] = MODULE
LOADER.exec_module(MODULE)


class ConfigTests(unittest.TestCase):
    def load(self, order, *, targets="192.0.2.21 2001:db8::22 storage", extra=""):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.env"
            path.write_text(
                "UPS_NAME=example-ups\nPRIMARY_HOST=192.0.2.10\n"
                f'SHUTDOWN_TARGETS="{targets}"\nSHUTDOWN_ORDER="{order}"\n{extra}',
                encoding="utf-8",
            )
            return MODULE.Config.load(path)

    def test_literal_addresses_and_single_host_step(self):
        config = self.load(
            "192.0.2.21,2001:0db8:0::22 STORAGE",
            extra='SHUTDOWN_DEPENDENCIES="192.0.2.21>storage storage>192.0.2.10"\n',
        )
        self.assertEqual(config.steps, (("192.0.2.21", "2001:db8::22"), ("storage",)))

    def test_rejects_invalid_inventory_order_and_dependency(self):
        cases = (
            ("192.0.2.21,2001:db8::22", {}),
            ("192.0.2.21,2001:db8::22 storage storage", {}),
            ("192.0.2.21,,2001:db8::22 storage", {}),
            ("192.0.2.21,2001:db8::22 unknown", {}),
            ("192.0.2.10", {"targets": "192.0.2.10"}),
            ("127.0.0.1", {"targets": "127.0.0.1"}),
            ("storage 192.0.2.21,2001:db8::22",
             {"extra": 'SHUTDOWN_DEPENDENCIES="192.0.2.21>storage"\n'}),
            ("192.0.2.21,storage 2001:db8::22",
             {"extra": 'SHUTDOWN_DEPENDENCIES="192.0.2.21>storage"\n'}),
            ("192.0.2.21,2001:db8::22 storage", {"extra": "ON_BATTERY_DELAY_SECONDS=-5\n"}),
        )
        for order, options in cases:
            with self.subTest(order=order, options=options):
                with self.assertRaises(ValueError):
                    self.load(order, **options)

    def test_promoted_storage_host_is_local_and_last(self):
        config = self.load(
            "192.0.2.21,2001:db8::22", targets="192.0.2.21 2001:db8::22",
            extra='SHUTDOWN_DEPENDENCIES="2001:db8::22>192.0.2.10"\n',
        )
        self.assertEqual(config.steps, (("192.0.2.21", "2001:db8::22"),))
        self.assertEqual(config.primary, "192.0.2.10")


class FakeClock:
    def __init__(self):
        self.elapsed = 0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        clock_patch = patch.object(MODULE, "time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_two_reads_cancel_recovery(self):
        for reason, readings in (
            ("onbattery", ({"OB"}, {"OL"})),
            ("commlost", (set(), {"OL"})),
        ):
            with self.subTest(reason=reason):
                self.clock.elapsed = 0
                with patch.object(MODULE, "ups_status", side_effect=readings) as status:
                    self.assertFalse(MODULE.verify_trigger(None, reason, lambda: False))
                    self.assertEqual(status.call_count, 2)
                    self.assertEqual(self.clock.elapsed, 5)

    def test_ping_can_advance_a_step_after_a_confirmed_request(self):
        with patch.object(MODULE, "ssh", return_value=True) as ssh, patch.object(
            MODULE, "ping", side_effect=(True, False, False, False),
        ):
            MODULE.shutdown_step(("192.0.2.21",), 180, lambda: False)
        ssh.assert_called_once_with("192.0.2.21", "poweroff", MODULE.SSH_TIMEOUT)
        self.assertLess(self.clock.elapsed, 180)

    def test_unreachable_or_unconfirmed_hosts_get_full_step_timeout(self):
        for initial_ping, ssh_result in ((False, True), (True, False), (None, True)):
            with self.subTest(initial_ping=initial_ping, ssh_result=ssh_result):
                self.clock.elapsed = 0
                with patch.object(MODULE, "ssh", return_value=ssh_result), patch.object(
                    MODULE, "ping", return_value=initial_ping,
                ):
                    MODULE.shutdown_step(("192.0.2.21",), 10, lambda: False)
                self.assertEqual(self.clock.elapsed, 10)

    def test_emergency_cuts_an_in_progress_wait(self):
        with patch.object(MODULE, "ssh", return_value=True) as ssh, patch.object(
            MODULE, "ping", return_value=True,
        ):
            MODULE.shutdown_step(("192.0.2.21", "192.0.2.22"), 180,
                                 lambda: self.clock.elapsed >= 3)
        self.assertEqual({call.args[0] for call in ssh.call_args_list}, {"192.0.2.21", "192.0.2.22"})
        self.assertEqual(self.clock.elapsed, 3)

    def test_recovery_timer_and_delays_generated_from_config(self):
        config = MODULE.Config("example", "primary", (), 38, 142, 180, True)
        output = Mock()
        with patch("sys.stdout", output):
            MODULE.render_upssched(config)
        content = output.write.call_args_list[0].args[0]
        self.assertIn("AT ONBATT * START-TIMER onbattery 38", content)
        self.assertIn("AT COMMBAD * START-TIMER commlost 142", content)
        self.assertIn("AT BATTERY_RECOVERED * START-TIMER recovered-battery 38", content)
        self.assertIn("AT COMMBAD * CANCEL-TIMER onbattery", content)

    def test_native_emergency_preserves_order_and_test_mode(self):
        steps = (("192.0.2.21", "192.0.2.22"), ("192.0.2.30",))
        fake_fcntl = SimpleNamespace(LOCK_EX=2, flock=Mock())
        actions = []

        def step(hosts, timeout, urgent):
            self.assertTrue(urgent())
            actions.append(hosts)

        def local_shutdown(args, timeout):
            actions.append("local")
            return SimpleNamespace(returncode=0)

        for test_mode in (True, False):
            with self.subTest(test_mode=test_mode), tempfile.TemporaryDirectory() as directory:
                actions.clear()
                config = MODULE.Config("example", "primary", steps, 60, 60, 180, test_mode)
                with (
                    patch.dict(sys.modules, {"fcntl": fake_fcntl}),
                    patch.object(MODULE, "RUN_DIR", Path(directory)),
                    patch.object(MODULE, "shutdown_step", side_effect=step),
                    patch.object(MODULE, "command", side_effect=local_shutdown),
                    patch.object(MODULE, "preflight", return_value=True) as preflight,
                ):
                    self.assertTrue(MODULE.orchestrate(config, "critical"))
                self.assertEqual(preflight.called, test_mode)
                self.assertEqual(actions, [] if test_mode else [*steps, "local"])


if __name__ == "__main__":
    unittest.main()
