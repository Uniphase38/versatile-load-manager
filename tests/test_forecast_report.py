from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from victron_load_manager.config import ScheduleConfig, SolarForecastConfig
from victron_load_manager.forecast_report import (
    CsvLock, VrmClient, complete_row, daily_energy, day_bounds, read_rows, snapshot, tick,
    write_rows,
)


ZONE = ZoneInfo("Europe/Paris")
FIELD_TOTALS = {
    "Bc": 6.858734395180363, "Gc": 8.263591282069683,
    "Pc": 2.7877640924416482, "Pb": 1.990717377193505,
    "Gb": 0.036408888176083565, "kwh": 19.937216035061283,
}


class ReportTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_vrm_access_token_daily_query(self):
        constants = SimpleNamespace(VRM_SITE_ID=123, VRM_API_URL="https://example.test/v2/",
                                    VRM_ACCESS_TOKEN=" test-token ")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = json.dumps({"success": True, "totals": FIELD_TOTALS}).encode()
        opener = Mock(return_value=response)
        client = VrmClient(constants, opener)
        for _ in range(2):
            result = client.fetch_day(date(2026, 10, 1), ZONE)
            self.assertAlmostEqual(result[0], 4.778481469635153)
        self.assertEqual(opener.call_count, 2)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertIn("start=1790805600", request.full_url)
        self.assertEqual(request.get_header("X-authorization"), "Token test-token")
        self.assertEqual(opener.call_args.kwargs, {"timeout": 15})

    @patch.dict(os.environ, {"VRM_ACCESS_TOKEN": "env-token"}, clear=True)
    def test_vrm_environment_with_old_ess_constants(self):
        constants = SimpleNamespace(VRM_SITE_ID=123, VRM_API_URL="https://example.test/v2")
        self.assertEqual(VrmClient(constants)._token, "env-token")

    @patch.dict(os.environ, {}, clear=True)
    def test_vrm_missing_token_and_unauthorized(self):
        constants = SimpleNamespace(VRM_SITE_ID=123, VRM_API_URL="https://example.test/v2")
        for token in (None, "", "   "):
            constants.VRM_ACCESS_TOKEN = token
            with self.assertRaisesRegex(ValueError, "Set VRM_ACCESS_TOKEN"):
                VrmClient(constants)
        constants.VRM_ACCESS_TOKEN = "secret-not-for-logs"
        opener = Mock(side_effect=HTTPError("https://example.test", 401, "Unauthorized", {}, None))
        with self.assertRaisesRegex(ValueError, "rejected.*401") as caught:
            VrmClient(constants, opener).fetch_day(date(2026, 10, 1), ZONE)
        self.assertNotIn(constants.VRM_ACCESS_TOKEN, str(caught.exception))
        opener.assert_called_once()
        opener.reset_mock()
        opener.side_effect = HTTPError("https://example.test", 503, "Unavailable", {}, None)
        with self.assertRaises(HTTPError):
            VrmClient(constants, opener).fetch_day(date(2026, 10, 1), ZONE)
        opener.assert_called_once()

    def test_real_vrm_sample_and_export(self):
        solar, consumption = daily_energy({"success": True, "totals": FIELD_TOTALS})
        self.assertAlmostEqual(solar, 4.778481469635153)
        self.assertAlmostEqual(consumption, 17.910089769691695)
        with_export = dict(FIELD_TOTALS, Pg=2.5)
        self.assertAlmostEqual(
            daily_energy({"success": True, "totals": with_export})[0], solar + 2.5,
        )

    def test_missing_measurement_is_not_zero(self):
        for invalid in (None, False, float("nan"), -1):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                daily_energy({"success": True, "totals": dict(FIELD_TOTALS, Bc=invalid)})
        with self.assertRaises(ValueError):
            daily_energy({"success": True, "totals": {}})

    def test_local_calendar_boundaries_include_dst(self):
        for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25),
                           (date(2026, 10, 1), 24)):
            start, end = day_bounds(day, ZONE)
            self.assertEqual(end + 1 - start, hours * 3600)
        self.assertEqual(day_bounds(date(2026, 10, 1), ZONE)[0], 1790805600)

    def test_first_forecast_is_frozen_including_zero(self):
        now = datetime(2026, 10, 2, 20, tzinfo=ZONE)
        forecast = SimpleNamespace(status="valid", tomorrow_kwh=0.0,
                                   fetched_at=now, source="open-meteo")
        rows = {}
        self.assertTrue(snapshot(rows, forecast, now))
        forecast.tomorrow_kwh = 42.1
        self.assertFalse(snapshot(rows, forecast, now))
        self.assertEqual(rows["2026-10-03"]["forecast_kwh"], "0.000")
        self.assertEqual(rows["2026-10-03"]["forecast_recorded_at"], now.isoformat())

    def test_past_data_has_no_invented_forecast_or_percentage_at_zero(self):
        rows = {}
        now = datetime(2026, 10, 2, 8, tzinfo=ZONE)
        complete_row(rows, date(2026, 10, 1), 4.77848, 17.91009, now)
        self.assertEqual(rows["2026-10-01"]["forecast_kwh"], "")
        self.assertEqual(rows["2026-10-01"]["solar_kwh"], "4.778")
        rows["2026-10-01"]["forecast_kwh"] = "5.000"
        complete_row(rows, date(2026, 10, 1), 0, 17, now)
        self.assertEqual(rows["2026-10-01"]["error_pct"], "")
        self.assertEqual(rows["2026-10-01"]["error_kwh"], "5.000")

    def test_csv_round_trip_and_lock(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            rows = {}
            complete_row(rows, date(2026, 10, 1), 4.77848, 17.91009,
                         datetime(2026, 10, 2, 8, tzinfo=ZONE))
            write_rows(path, rows)
            self.assertEqual(read_rows(path), rows)
            with CsvLock(path):
                with self.assertRaises(ValueError):
                    with CsvLock(path):
                        pass
            with CsvLock(path):
                pass

    def test_once_per_day_and_failure_preserves_existing_data(self):
        config = SimpleNamespace(schedule=ScheduleConfig(timezone="Europe/Paris"))
        vrm = Mock()
        vrm.fetch_day.return_value = (4.77848, 17.91009)
        now = datetime(2026, 10, 2, 8, tzinfo=ZONE)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            self.assertTrue(tick(path, config, vrm, now, time(20), time(8)))
            self.assertEqual(vrm.fetch_day.call_count, 2)
            self.assertTrue(tick(path, config, vrm, now, time(20), time(8)))
            self.assertEqual(vrm.fetch_day.call_count, 2)
            previous = read_rows(path)
            vrm.fetch_day.side_effect = OSError("network unavailable")
            self.assertFalse(tick(path, config, vrm, now, time(20), time(8), date(2026, 10, 1)))
            self.assertEqual(read_rows(path), previous)

    def test_snapshot_schedule_then_next_day_completion(self):
        config = SimpleNamespace(schedule=ScheduleConfig(timezone="Europe/Paris"),
                                 solar_forecast=SolarForecastConfig(enabled=True))
        vrm = Mock()
        vrm.fetch_day.return_value = (4.77848, 17.91009)
        evening = datetime(2026, 10, 2, 20, tzinfo=ZONE)
        forecast = SimpleNamespace(status="valid", tomorrow_kwh=6.23,
                                   fetched_at=evening, source="open-meteo")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.csv"
            with patch("victron_load_manager.forecast_report.SolarForecastClient") as client:
                client.return_value.read.return_value = forecast
                self.assertTrue(tick(path, config, vrm, evening, time(20), time(8)))
                self.assertTrue(tick(path, config, vrm, evening, time(20), time(8)))
                self.assertEqual(client.call_count, 1)
                self.assertNotEqual(client.call_args.args[0].cache_file,
                                    config.solar_forecast.cache_file)
            morning = datetime(2026, 10, 4, 8, tzinfo=ZONE)
            self.assertTrue(tick(path, config, vrm, morning, time(20), time(8)))
            row = read_rows(path)["2026-10-03"]
            self.assertEqual(row["forecast_kwh"], "6.230")
            self.assertEqual(row["error_kwh"], "1.452")


if __name__ == "__main__":
    unittest.main()
