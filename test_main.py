import threading
import time
import unittest
from unittest import mock

import main


class _HungChannel:
    def __init__(self):
        self.closed = False

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return False

    def close(self):
        self.closed = True


class _FrameChannel(_HungChannel):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame.encode("utf-8")

    def recv_ready(self):
        return bool(self.frame)

    def recv(self, size):
        chunk = self.frame[:size]
        self.frame = self.frame[size:]
        return chunk


class _Stream:
    def __init__(self, channel):
        self.channel = channel


class _HungClient:
    def __init__(self):
        self.channel = _HungChannel()

    def exec_command(self, *args, **kwargs):
        return None, _Stream(self.channel), None


class _ActiveTransport:
    def is_active(self):
        return True


class _ActiveClient:
    def __init__(self):
        self.closed = False

    def get_transport(self):
        return _ActiveTransport()

    def close(self):
        self.closed = True


class FallbackProbeTests(unittest.TestCase):
    def test_probe_classifies_unreachable_and_driver_errors(self):
        output = "\n".join(
            [
                "BOOT\tboot-a",
                "GPU\t0000:32:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                "GPU\t0000:33:00.0\t0x2204\tunbound\tactive\tff\tunreachable",
                "GPU\t0000:57:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                "KNOWN\t0000:32:00.0",
                "KNOWN\t0000:33:00.0",
                "KNOWN\t0000:57:00.0",
                "KERNEL\tNVRM: GPU 0000:57:00.0: rm_init_adapter failed",
            ]
        )

        probe = main.parse_pci_fallback_probe(output, set())
        states = {item["bdf"]: item["state"] for item in probe["devices"]}

        self.assertEqual(states["0000:32:00.0"], "pci_present")
        self.assertEqual(states["0000:33:00.0"], "pci_unreachable")
        self.assertEqual(states["0000:57:00.0"], "driver_error")
        self.assertFalse(probe["all_ready"])
        self.assertFalse(probe["recovery_ready"])

    def test_probe_remembers_devices_that_disappear(self):
        probe = main.parse_pci_fallback_probe(
            "BOOT\tboot-b\n",
            {"0000:65:00.0"},
        )

        self.assertEqual(probe["devices"][0]["state"], "missing")
        self.assertFalse(probe["recovery_ready"])

    def test_driver_error_is_still_eligible_for_rate_limited_retry(self):
        output = "\n".join(
            [
                "GPU\t0000:57:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                "KERNEL\tNVRM: GPU 0000:57:00.0: rm_init_adapter failed",
            ]
        )

        probe = main.parse_pci_fallback_probe(output, set())

        self.assertFalse(probe["all_ready"])
        self.assertTrue(probe["recovery_ready"])

    def test_noncritical_xid_is_informational_only(self):
        output = "\n".join(
            [
                "GPU\t0000:57:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                "KERNEL\tNVRM: Xid (PCI:0000:57:00): 31, pid=1234",
            ]
        )

        probe = main.parse_pci_fallback_probe(output, set())

        self.assertEqual(probe["devices"][0]["state"], "pci_present")
        self.assertTrue(probe["all_ready"])

    def test_critical_xid_marks_driver_error(self):
        output = "\n".join(
            [
                "GPU\t0000:57:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                "KERNEL\tNVRM: Xid (PCI:0000:57:00): 79, GPU has fallen off",
            ]
        )

        probe = main.parse_pci_fallback_probe(output, set())

        self.assertEqual(probe["devices"][0]["state"], "driver_error")
        self.assertFalse(probe["all_ready"])

    def test_fallback_text_contains_device_states(self):
        probe = main.parse_pci_fallback_probe(
            "\n".join(
                [
                    "GPU\t0000:32:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                    "GPU\t0000:33:00.0\t0x2204\tunbound\tactive\tff\tunreachable",
                ]
            ),
            set(),
        )

        text = main.format_pci_fallback_text(
            probe,
            "gpustat timeout",
            "2026-09-23T00:00:00+00:00",
            "2026-09-22T23:59:00+00:00",
        )

        self.assertIn("GPUSTAT DEGRADED MODE", text)
        self.assertIn("0000:33:00.0", text)
        self.assertIn("\x1b[91mPCI_UNREACHABLE", text)
        self.assertIn("\x1b[92mPCI_PRESENT", text)
        self.assertIn("Last healthy at:\x1b[0m 2026-09-22T23:59:00+00:00", text)
        html = main.ansi_to_html_fragment(text)
        self.assertNotIn("\x1b", html)
        self.assertIn("PCI_UNREACHABLE", html)
        self.assertIn("PCI_PRESENT", html)

    def test_remote_command_timeout_closes_the_channel(self):
        client = _HungClient()
        started_at = time.monotonic()

        with self.assertRaises(main.RemoteCommandTimeout):
            main.run_remote_command_with_timeout(
                client,
                "hung-command",
                0.01,
                threading.Event(),
            )

        self.assertTrue(client.channel.closed)
        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_polling_switches_from_pending_to_degraded_output(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        saved_results = []
        probe = main.parse_pci_fallback_probe(
            "GPU\t0000:33:00.0\t0x2204\tunbound\tactive\tff\tunreachable",
            set(),
        )

        def capture_result(**kwargs):
            saved_results.append(kwargs)
            if kwargs["status"] == "degraded":
                server_stop_event.set()

        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(main, "fetch_latest_collector_state", return_value={}),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(
                main,
                "fetch_gpustat_json_bounded",
                side_effect=main.RemoteCommandTimeout("gpustat timeout"),
            ),
            mock.patch.object(main, "probe_pci_fallback", return_value=probe),
            mock.patch.object(main, "save_poll_result", side_effect=capture_result),
            mock.patch.object(main, "record_collector_event"),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        self.assertTrue(client.closed)
        self.assertEqual(len(saved_results), 1)
        self.assertEqual(saved_results[0]["status"], "degraded")
        self.assertEqual(saved_results[0]["connection_status"], "connected")
        self.assertIn("GPUSTAT DEGRADED MODE", saved_results[0]["stdout"])
        self.assertIn("PCI_UNREACHABLE", saved_results[0]["stdout"])

    def test_polling_recovers_after_boot_and_pci_state_change(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        failed_probe = main.parse_pci_fallback_probe(
            "\n".join(
                [
                    "BOOT\tboot-a",
                    "GPU\t0000:33:00.0\t0x2204\tunbound\tactive\tff\tunreachable",
                ]
            ),
            set(),
        )
        recovered_probe = main.parse_pci_fallback_probe(
            "\n".join(
                [
                    "BOOT\tboot-b",
                    "GPU\t0000:33:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
                ]
            ),
            {"0000:33:00.0"},
        )
        gpustat_calls = 0

        def fetch_gpustat(*args, **kwargs):
            nonlocal gpustat_calls
            gpustat_calls += 1
            if gpustat_calls == 1:
                raise main.RemoteCommandTimeout("gpustat timeout")
            return '{"gpus": []}', "", 0

        watch_channel = _FrameChannel(
            "Wed Sep 23 10:00:00 2026\n[0] NVIDIA GPU | 30'C, 0 %\n"
        )

        def start_watch(*args, **kwargs):
            stream = _Stream(watch_channel)
            return stream, stream

        saved_results = []

        def capture_result(**kwargs):
            saved_results.append(kwargs)
            if kwargs["status"] == "ok":
                server_stop_event.set()

        events = []

        def capture_event(server, event_type, reason="", details=None):
            events.append(event_type)

        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(main, "fetch_latest_collector_state", return_value={}),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(
                main,
                "fetch_gpustat_json_bounded",
                side_effect=fetch_gpustat,
            ),
            mock.patch.object(
                main,
                "probe_pci_fallback",
                side_effect=[failed_probe, recovered_probe, recovered_probe],
            ),
            mock.patch.object(main, "start_watch_streams", side_effect=start_watch),
            mock.patch.object(
                main,
                "save_poll_result",
                side_effect=capture_result,
            ),
            mock.patch.object(main, "save_json_poll_result"),
            mock.patch.object(
                main,
                "save_text_poll_result",
                side_effect=capture_result,
            ),
            mock.patch.object(
                main,
                "record_collector_event",
                side_effect=capture_event,
            ),
            mock.patch.object(main, "FALLBACK_POLL_INTERVAL_SECONDS", 0),
            mock.patch.object(main, "GPUSTAT_RECOVERY_CONFIRM_INTERVAL_SECONDS", 0),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        self.assertEqual(gpustat_calls, 3)
        self.assertIn("recovered", events)
        self.assertEqual(saved_results[-1]["status"], "ok")
        self.assertTrue(watch_channel.closed)
        self.assertTrue(client.closed)

    def test_three_consecutive_soft_failures_enter_degraded_mode(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        probe = main.parse_pci_fallback_probe(
            "GPU\t0000:33:00.0\t0x2204\tnvidia\tactive\ta1\tpci-visible",
            set(),
        )
        saved_results = []
        events = []

        def capture_result(**kwargs):
            saved_results.append(kwargs)
            if kwargs["status"] == "degraded":
                server_stop_event.set()

        def capture_event(server, event_type, reason="", details=None):
            events.append((event_type, details or {}))

        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(main, "fetch_latest_collector_state", return_value={}),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(
                main,
                "fetch_gpustat_json_bounded",
                side_effect=RuntimeError("temporary gpustat error"),
            ) as gpustat_mock,
            mock.patch.object(main, "probe_pci_fallback", return_value=probe),
            mock.patch.object(main, "save_poll_result", side_effect=capture_result),
            mock.patch.object(
                main,
                "record_collector_event",
                side_effect=capture_event,
            ),
            mock.patch.object(main, "POLL_INTERVAL_SECONDS", 0),
            mock.patch.object(main, "FALLBACK_POLL_INTERVAL_SECONDS", 0),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        self.assertEqual(gpustat_mock.call_count, 3)
        self.assertEqual(saved_results[-1]["status"], "degraded")
        self.assertEqual(
            [
                details["failure_count"]
                for event, details in events
                if event == "gpustat_soft_failure"
            ],
            [1, 2, 3],
        )
        self.assertEqual(
            [event for event, _ in events].count("degraded_entered"),
            1,
        )

    def test_success_resets_consecutive_soft_failure_count(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        watch_channel = _HungChannel()
        stream = _Stream(watch_channel)
        soft_error = RuntimeError("temporary gpustat error")
        events = []

        def capture_event(server, event_type, reason="", details=None):
            events.append((event_type, details or {}))
            soft_events = [
                event
                for event, _ in events
                if event == "gpustat_soft_failure"
            ]
            if len(soft_events) == 4:
                server_stop_event.set()

        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(main, "fetch_latest_collector_state", return_value={}),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(
                main,
                "fetch_gpustat_json_bounded",
                side_effect=[
                    soft_error,
                    soft_error,
                    ('{"gpus": []}', "", 0),
                    soft_error,
                    soft_error,
                ],
            ),
            mock.patch.object(
                main,
                "start_watch_streams",
                return_value=(stream, stream),
            ),
            mock.patch.object(main, "save_poll_result"),
            mock.patch.object(main, "save_json_poll_result"),
            mock.patch.object(main, "save_text_poll_result"),
            mock.patch.object(
                main,
                "record_collector_event",
                side_effect=capture_event,
            ),
            mock.patch.object(main, "POLL_INTERVAL_SECONDS", 0),
            mock.patch.object(main, "GPUSTAT_WATCH_STALE_SECONDS", 999),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        self.assertEqual(
            [
                details["failure_count"]
                for event, details in events
                if event == "gpustat_soft_failure"
            ],
            [1, 2, 1, 2],
        )
        self.assertNotIn("degraded_entered", [event for event, _ in events])

    def test_stale_watch_restarts_without_entering_degraded_mode(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        watch_channel = _HungChannel()
        stream = _Stream(watch_channel)
        saved_results = []
        events = []

        def capture_event(server, event_type, reason="", details=None):
            events.append(event_type)
            if event_type == "watch_restarted":
                server_stop_event.set()

        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(main, "fetch_latest_collector_state", return_value={}),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(
                main,
                "fetch_gpustat_json_bounded",
                return_value=('{"gpus": []}', "", 0),
            ),
            mock.patch.object(
                main,
                "start_watch_streams",
                return_value=(stream, stream),
            ),
            mock.patch.object(
                main,
                "save_poll_result",
                side_effect=lambda **kwargs: saved_results.append(kwargs),
            ),
            mock.patch.object(main, "save_json_poll_result"),
            mock.patch.object(main, "save_text_poll_result"),
            mock.patch.object(
                main,
                "record_collector_event",
                side_effect=capture_event,
            ),
            mock.patch.object(main, "POLL_INTERVAL_SECONDS", 0),
            mock.patch.object(main, "GPUSTAT_WATCH_STALE_SECONDS", 0),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        self.assertIn("watch_restarted", events)
        self.assertNotIn("degraded_entered", events)
        self.assertFalse(any(item["status"] == "degraded" for item in saved_results))
        self.assertTrue(watch_channel.closed)

    def test_polling_preserves_degraded_circuit_breaker_after_restart(self):
        server_stop_event = threading.Event()
        client = _ActiveClient()
        previous_output = "\n".join(
            [
                "GPUSTAT DEGRADED MODE",
                "Boot ID: boot-a",
                "0000:33:00.0    10de:2204   unbound  ff  active  PCI_UNREACHABLE",
            ]
        )
        failed_probe = main.parse_pci_fallback_probe(
            "\n".join(
                [
                    "BOOT\tboot-a",
                    "GPU\t0000:33:00.0\t0x2204\tunbound\tactive\tff\tunreachable",
                ]
            ),
            {"0000:33:00.0"},
        )

        def stop_after_fallback(**kwargs):
            server_stop_event.set()

        gpustat_mock = mock.Mock()
        with (
            mock.patch.object(main, "fetch_last_success_at", return_value=None),
            mock.patch.object(
                main,
                "fetch_latest_collector_state",
                return_value={
                    "status": "degraded",
                    "stdout": previous_output,
                    "stderr": "previous timeout",
                },
            ),
            mock.patch.object(main, "connect_ssh_client", return_value=client),
            mock.patch.object(main, "fetch_gpustat_json_bounded", gpustat_mock),
            mock.patch.object(main, "probe_pci_fallback", return_value=failed_probe),
            mock.patch.object(
                main,
                "save_poll_result",
                side_effect=stop_after_fallback,
            ),
            mock.patch.object(main, "record_collector_event"),
        ):
            main.poll_server_forever(
                {"name": "gpu-test", "ip": "192.0.2.10", "username": "monitor"},
                server_stop_event,
            )

        gpustat_mock.assert_not_called()
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
