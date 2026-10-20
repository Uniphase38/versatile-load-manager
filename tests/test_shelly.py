import unittest
import sys
from types import ModuleType, SimpleNamespace

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests = ModuleType("requests")
    requests.Session = object
    requests.exceptions = SimpleNamespace(RequestException=Exception)
    sys.modules["requests"] = requests

from versatile_load_manager.shelly import (
    ShellyClient,
    ShellyCommandResult,
    ShellyError,
    ShellyStatus,
)


class FakeResponse:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, rpc=None, get=None):
        self.rpc = rpc or {}
        self.gets = get or {}
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json, timeout))
        value = self.rpc.get(json["method"])
        if isinstance(value, Exception):
            raise value
        return FakeResponse({"id": 1, "result": value})

    def get(self, url, timeout):
        self.calls.append(("GET", url, timeout))
        value = self.gets.get(url.rsplit("/", 1)[-1])
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


class ShellyReadOnlyTests(unittest.TestCase):
    def test_reads_gen2_switch_and_power(self):
        session = FakeSession(rpc={
            "Shelly.GetDeviceInfo": {"gen": 2, "model": "SNSW-001P16EU"},
            "Shelly.GetStatus": {
                "switch:0": {"id": 0, "output": True, "apower": 2198.4}
            },
        })
        status = ShellyClient("cumulus.local", session=session).read_status()
        self.assertEqual(
            status,
            ShellyStatus("cumulus.local", 2, "SNSW-001P16EU", True, 2198.4),
        )
        self.assertTrue(all(call[0] == "POST" for call in session.calls))
        self.assertTrue(all(call[2]["method"].endswith(("GetDeviceInfo", "GetStatus")) for call in session.calls))

    def test_falls_back_to_gen1(self):
        session = FakeSession(
            rpc={"Shelly.GetDeviceInfo": OSError("RPC unavailable")},
            get={
                "shelly": {"type": "SHPLG-S"},
                "status": {"relays": [{"ison": False}], "meters": [{"power": 0}]},
            },
        )
        status = ShellyClient("plug.local", session=session).read_status()
        self.assertEqual(status, ShellyStatus("plug.local", 1, "SHPLG-S", False, 0.0))

    def test_power_is_optional_for_non_metered_device(self):
        session = FakeSession(rpc={
            "Shelly.GetDeviceInfo": {"gen": 3, "model": "S3SW-001X16EU"},
            "Shelly.GetStatus": {"switch:0": {"id": 0, "output": False}},
        })
        status = ShellyClient("relay.local", session=session).read_status()
        self.assertEqual(status.generation, 3)
        self.assertIsNone(status.power_w)

    def test_reads_pm_mini_as_meter_without_switch(self):
        session = FakeSession(rpc={
            "Shelly.GetDeviceInfo": {"gen": 3, "model": "S3PM-001PCEU16"},
            "Shelly.GetStatus": {"pm1:0": {"id": 0, "apower": 412.7}},
        })
        status = ShellyClient("meter.local", session=session).read_status()
        self.assertEqual(status.component, "pm1")
        self.assertIsNone(status.output)
        self.assertEqual(status.power_w, 412.7)

    def test_unreachable_device_reports_both_attempts(self):
        session = FakeSession(
            rpc={"Shelly.GetDeviceInfo": OSError("timeout")},
            get={"shelly": OSError("timeout")},
        )
        with self.assertRaisesRegex(ShellyError, "Gen2.*Gen1"):
            ShellyClient("offline.local", session=session).read_status()


class ShellyCommandTests(unittest.TestCase):
    @staticmethod
    def _switch_session(outputs):
        values = iter(outputs)

        def status():
            return {"switch:0": {"id": 0, "output": next(values), "apower": 0}}

        class StatefulSession(FakeSession):
            def post(self, url, json, timeout):
                self.calls.append(("POST", url, json, timeout))
                if json["method"] == "Shelly.GetDeviceInfo":
                    result = {"gen": 2, "model": "SNSW-001P16EU"}
                elif json["method"] == "Shelly.GetStatus":
                    result = status()
                elif json["method"] == "Switch.Set":
                    result = {"was_on": False}
                else:
                    result = None
                return FakeResponse({"id": 1, "result": result})

        return StatefulSession()

    def test_dry_run_never_sends_switch_set(self):
        session = self._switch_session([False])
        result = ShellyClient("load.local", session=session).set_output(True, dry_run=True)
        self.assertEqual(
            result, ShellyCommandResult("load.local", True, False, False, False, True)
        )
        self.assertNotIn("Switch.Set", [call[2]["method"] for call in session.calls])

    def test_already_correct_is_idempotent(self):
        session = self._switch_session([True])
        result = ShellyClient("load.local", session=session).set_output(True, dry_run=False)
        self.assertFalse(result.command_sent)
        self.assertNotIn("Switch.Set", [call[2]["method"] for call in session.calls])

    def test_real_command_is_sent_and_confirmed(self):
        session = self._switch_session([False, True])
        result = ShellyClient("load.local", session=session).set_output(True, dry_run=False)
        self.assertTrue(result.command_sent)
        self.assertTrue(result.observed_output)
        self.assertIn("Switch.Set", [call[2]["method"] for call in session.calls])

    def test_unconfirmed_command_fails(self):
        session = self._switch_session([False, False])
        with self.assertRaisesRegex(ShellyError, "did not confirm"):
            ShellyClient("load.local", session=session).set_output(True, dry_run=False)

    def test_pm_meter_is_never_switchable(self):
        session = FakeSession(rpc={
            "Shelly.GetDeviceInfo": {"gen": 3, "model": "S3PM-001PCEU16"},
            "Shelly.GetStatus": {"pm1:0": {"id": 0, "apower": 0}},
        })
        with self.assertRaisesRegex(ShellyError, "not switchable"):
            ShellyClient("meter.local", session=session).set_output(True, dry_run=True)


if __name__ == "__main__":
    unittest.main()
