"""Supervisor decisions with fake children, signals, time and a private temporary root."""

import contextlib
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis_office import private_service as service

MARKER = "PRIVATE_EXCEPTION_PAYLOAD_MUST_NOT_APPEAR"


class PrivateServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(service, "private_root", return_value=self.root))
        self.stack.enter_context(patch.object(service.sys, "argv", ["private_service", "run"]))
        self.handlers = {}
        self.stack.enter_context(
            patch.object(
                service.signal,
                "signal",
                side_effect=lambda number, handler: self.handlers.update({number: handler}),
            )
        )
        self.clock = 0.0
        self.sleeps = []
        self.stack.enter_context(
            patch.object(
                service,
                "time",
                SimpleNamespace(monotonic=lambda: self.clock, sleep=self.sleep),
            )
        )
        self.popen = self.stack.enter_context(patch.object(service.subprocess, "Popen"))
        self.stderr = self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.atomic_json = service.atomic_json

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.clock = round(self.clock + duration, 6)

    def child(self, code=0, wait=None, poll=None):
        return SimpleNamespace(
            pid=12345,
            wait=Mock(side_effect=wait, return_value=code),
            poll=Mock(return_value=poll),
            terminate=Mock(),
        )

    def journal(self):
        return json.loads((self.root / "state/private-supervisor-events.json").read_text())

    def state(self):
        return json.loads((self.root / "state/private-service.json").read_text())

    def assert_private(self):
        for file in (self.root / "state").iterdir():
            self.assertNotIn(MARKER, file.read_text())
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertLess(file.stat().st_size, 65536)
        self.assertNotIn(MARKER, self.stderr.getvalue())

    def test_exit_codes_retry_limit_delay_and_off_launch_contract(self):
        for codes, result, delay in (([0], 0, 0), ([-9, 3, 0], 0, 20), ([1, 2, 3], 1, 30)):
            with self.subTest(codes=codes):
                self.clock = 0
                self.sleeps.clear()
                self.popen.reset_mock()
                self.popen.side_effect = [self.child(code) for code in codes]
                self.assertEqual(service.main(), result)
                self.assertEqual(self.popen.call_count, len(codes))
                self.assertEqual(self.clock, delay)
                self.assertTrue(all(item == 0.2 for item in self.sleeps))
                for call in self.popen.call_args_list:
                    self.assertEqual(
                        call.args[0],
                        [
                            service.sys.executable,
                            "-I",
                            "-B",
                            "-m",
                            "jarvis_office.echo.gateway",
                            "--config",
                            str(self.root / "config/private-gateway.toml"),
                        ],
                    )
                    self.assertEqual(call.kwargs, {"cwd": self.root})
                events = self.journal()["events"]
                self.assertEqual(
                    [
                        event["child_exit_code"]
                        for event in events
                        if event["event"] == "CHILD_WAIT_RETURNED"
                    ],
                    codes,
                )
                self.assertEqual(events[-1]["supervisor_exit_code"], result)
                self.assertEqual(self.state()["exit_code"], codes[-1])
                self.assert_private()
        specification = service.specification()
        self.assertFalse(specification["KeepAlive"])
        self.assertEqual(specification["StandardErrorPath"], "/dev/null")

    def test_stop_requests_terminate_only_a_live_owned_child(self):
        for poll, code, terminates in ((None, -15, 1), (0, 0, 0)):
            with self.subTest(poll=poll):
                child = self.child(code, poll=poll)
                child.wait.side_effect = lambda code=code: (
                    self.handlers[signal.SIGTERM](15, None),
                    code,
                )[1]
                self.popen.side_effect = None
                self.popen.return_value = child
                self.popen.reset_mock()
                self.assertEqual(service.main(), code)
                self.assertEqual(child.terminate.call_count, terminates)
                self.assertEqual(self.popen.call_count, 1)
                self.assertEqual(self.sleeps, [])
                self.assertEqual(self.state()["state"], "STOPPED")
                events = self.journal()["events"]
                stop = next(event for event in events if event["event"] == "STOP_REQUESTED")
                self.assertIsNone(stop["child_exit_code"])
                self.assertTrue(events[-1]["stop_requested"])

    def test_terminate_race_preserves_error_and_unknown_child_exit(self):
        child = self.child()
        child.terminate.side_effect = ProcessLookupError(MARKER)
        child.wait.side_effect = lambda: self.handlers[signal.SIGINT](2, None)
        self.popen.return_value = child
        self.assertEqual(service.main(), 1)
        self.assertEqual(self.popen.call_count, 1)
        event = self.journal()["events"][-1]
        self.assertEqual(event["phase"], "STOP_TERMINATE")
        self.assertEqual(event["exception_class"], "ProcessLookupError")
        self.assertIsNone(event["child_exit_code"])
        self.assertEqual(self.state()["state"], "DEGRADED")
        self.assert_private()

    def test_stop_during_retry_delay_keeps_original_nonzero_supervisor_result(self):
        child = self.child(7, poll=7)
        self.popen.return_value = child

        def sleep(duration):
            self.sleep(duration)
            self.handlers[signal.SIGTERM](15, None)

        with patch.object(service.time, "sleep", side_effect=sleep):
            self.assertEqual(service.main(), 1)
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(child.terminate.call_count, 0)
        self.assertEqual(self.sleeps, [0.2])
        event = self.journal()["events"][-1]
        self.assertEqual(event["phase"], "RETRY_DELAY")
        self.assertEqual(event["child_exit_code"], 7)
        self.assertEqual(event["supervisor_exit_code"], 1)
        self.assertEqual(event["attempt"], 1)

    def test_stop_before_first_attempt_keeps_attempt_zero(self):
        def register(number, handler):
            self.handlers[number] = handler
            if number == signal.SIGTERM:
                handler(number, None)

        with patch.object(service.signal, "signal", side_effect=register):
            self.assertEqual(service.main(), 1)
        self.popen.assert_not_called()
        self.assertEqual(self.journal()["events"][-1]["attempt"], 0)

    def test_launch_wait_and_state_errors_keep_observed_boundaries(self):
        for phase, child_code, launches in (
            ("CHILD_START", None, 1),
            ("CHILD_WAIT", None, 1),
            ("STATE_START", None, 0),
            ("STATE_EXIT", 2, 1),
        ):
            with self.subTest(phase=phase):
                error = OSError(MARKER)
                child = self.child(2, wait=error if phase == "CHILD_WAIT" else None)
                self.popen.reset_mock()
                self.popen.side_effect = error if phase == "CHILD_START" else None
                self.popen.return_value = child
                failed = False

                def write(path, value, phase=phase, error=error):
                    nonlocal failed
                    if path.name == "private-service.json" and not failed:
                        target = "STARTING" if phase == "STATE_START" else "DEGRADED"
                        if phase.startswith("STATE_") and value["state"] == target:
                            failed = True
                            raise error
                    self.atomic_json(path, value)

                with patch.object(service, "atomic_json", side_effect=write):
                    self.assertEqual(service.main(), 1)
                event = self.journal()["events"][-1]
                self.assertEqual(event["event"], "SUPERVISOR_ERROR")
                self.assertEqual(event["phase"], phase)
                self.assertEqual(event["child_exit_code"], child_code)
                self.assertEqual(self.popen.call_count, launches)
                self.assertEqual(self.state()["exit_code"], child_code)
                self.assertEqual(self.state()["error"], "SUPERVISOR_ERROR")
                self.assertEqual(self.sleeps, [])
                self.assert_private()

    def test_terminal_state_failure_does_not_mask_original_exception(self):
        original = OSError(MARKER)
        self.popen.side_effect = original

        def write(path, value):
            if path.name == "private-service.json" and value.get("error"):
                raise PermissionError("SECOND_" + MARKER)
            self.atomic_json(path, value)

        with patch.object(service, "atomic_json", side_effect=write):
            with self.assertRaises(OSError) as caught:
                service.supervise()
        self.assertIs(caught.exception, original)
        events = self.journal()["events"]
        self.assertEqual(events[-2]["phase"], "CHILD_START")
        self.assertEqual(events[-1]["event"], "TERMINAL_STATE_WRITE_FAILED")
        self.assertEqual(events[-1]["phase"], "STATE_ERROR")
        self.assertIsNone(events[-1]["child_exit_code"])
        self.assert_private()

    def test_unavailable_journal_cannot_change_success_or_primary_failure(self):
        def write(path, value):
            if path.name == "private-supervisor-events.json":
                raise PermissionError(MARKER)
            self.atomic_json(path, value)

        with patch.object(service, "atomic_json", side_effect=write):
            self.popen.return_value = self.child()
            self.assertEqual(service.main(), 0)
            self.popen.side_effect = OSError(MARKER)
            self.assertEqual(service.main(), 1)
        self.assertEqual(self.state()["error"], "SUPERVISOR_ERROR")
        self.assertEqual(self.stderr.getvalue(), "PRIVATE_SERVICE_OPERATION_FAILED\n")
        self.assertFalse((self.root / "state/private-supervisor-events.json").exists())
        self.assert_private()

    def test_bounded_journal_excludes_exception_messages_and_custom_class_names(self):
        child = self.child(poll=0)

        def wait():
            for _ in range(100):
                self.handlers[signal.SIGTERM](15, None)
            raise type(MARKER, (ValueError,), {})(MARKER)

        child.wait.side_effect = wait
        self.popen.return_value = child
        self.assertEqual(service.main(), 1)
        journal = self.journal()
        self.assertEqual(len(journal["events"]), 64)
        self.assertGreater(journal["dropped_events"], 0)
        self.assertEqual(journal["events"][-1]["exception_class"], "ValueError")
        self.assertEqual(child.terminate.call_count, 0)
        self.assertEqual(
            sorted(path.name for path in (self.root / "state").iterdir()),
            ["private-service.json", "private-supervisor-events.json"],
        )
        self.assert_private()

    def test_recovered_journal_reports_write_loss(self):
        failed = False

        def write(path, value):
            nonlocal failed
            if path.name == "private-supervisor-events.json" and not failed:
                failed = True
                raise OSError(MARKER)
            self.atomic_json(path, value)

        self.popen.return_value = self.child()
        with patch.object(service, "atomic_json", side_effect=write):
            self.assertEqual(service.main(), 0)
        self.assertEqual(self.journal()["write_failures"], 1)
        self.assertEqual(self.journal()["events"][0]["event"], "SUPERVISOR_START")

    def test_restart_preserves_previous_exit_and_failed_rotation_preserves_it(self):
        self.popen.side_effect = OSError(MARKER)
        self.assertEqual(service.main(), 1)
        current = self.root / "state/private-supervisor-events.json"
        before = current.read_bytes()
        self.popen.side_effect = None
        self.popen.return_value = self.child()
        self.assertEqual(service.main(), 0)
        previous = self.root / "state/private-supervisor-events.previous.json"
        self.assertEqual(previous.read_bytes(), before)
        new_current = current.read_bytes()
        replace = service.os.replace

        def fail_rotation(source, destination):
            if Path(source) == current:
                raise PermissionError(MARKER)
            replace(source, destination)

        with patch.object(service.os, "replace", side_effect=fail_rotation):
            self.assertEqual(service.main(), 0)
        self.assertEqual(current.read_bytes(), new_current)
        self.assertEqual(previous.read_bytes(), before)
        self.assert_private()


if __name__ == "__main__":
    unittest.main()
