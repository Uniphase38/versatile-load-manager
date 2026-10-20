import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from versatile_load_manager.config import SolarForecastConfig
from versatile_load_manager.solar_forecast import (
    SolarForecastClient, _open_meteo_azimuth,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SolarForecastTests(unittest.TestCase):
    zone = ZoneInfo("Europe/Paris")

    def config(self, cache_file):
        return SolarForecastConfig(
            enabled=True,
            latitude=45.0,
            longitude=4.0,
            peak_power_kwp=8,
            tilt_deg=24,
            azimuth_deg=217,
            performance_ratio=0.85,
            cache_file=str(cache_file),
        )

    def test_geographic_azimuth_is_converted_for_open_meteo(self):
        self.assertEqual(_open_meteo_azimuth(217), 37)
        self.assertEqual(_open_meteo_azimuth(180), 0)

    def test_forecast_is_numeric_and_uses_tilted_irradiance(self):
        with tempfile.TemporaryDirectory() as directory:
            requested = {}
            payload = {
                "hourly": {
                    "time": [
                        "2026-09-22T10:00", "2026-09-22T11:00",
                        "2026-09-23T10:00",
                    ],
                    "global_tilted_irradiance": [500, 500, 1000],
                }
            }

            def opener(request, timeout):
                requested["url"] = request.full_url
                requested["timeout"] = timeout
                return FakeResponse(payload)

            client = SolarForecastClient(
                self.config(Path(directory) / "forecast.json"), self.zone, opener,
                background=False,
            )
            result = client.read(datetime(2026, 9, 22, 10, 30, tzinfo=self.zone))

            self.assertEqual(result.status, "valid")
            self.assertAlmostEqual(result.today_kwh, 6.8)
            self.assertAlmostEqual(result.remaining_today_kwh, 3.4)
            self.assertAlmostEqual(result.tomorrow_kwh, 6.8)
            query = parse_qs(urlparse(requested["url"]).query)
            self.assertEqual(query["hourly"], ["global_tilted_irradiance"])
            self.assertEqual(query["tilt"], ["24"])
            self.assertEqual(query["azimuth"], ["37.0"])

    def test_fresh_cache_survives_api_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "forecast.json"
            config = self.config(cache)
            now = datetime(2026, 9, 22, 10, 30, tzinfo=self.zone)
            payload = {
                "hourly": {
                    "time": ["2026-09-22T10:00", "2026-09-23T10:00"],
                    "global_tilted_irradiance": [500, 1000],
                }
            }
            first = SolarForecastClient(
                config, self.zone, lambda *_args, **_kwargs: FakeResponse(payload),
                background=False,
            )
            self.assertEqual(first.read(now).status, "valid")

            def unavailable(*_args, **_kwargs):
                raise OSError("network down")

            second = SolarForecastClient(
                config, self.zone, unavailable, background=False
            )
            result = second.read(now + timedelta(hours=4))
            self.assertEqual(result.status, "valid")
            self.assertEqual(result.source, "cache")
            self.assertEqual(result.error, "network down")
            self.assertAlmostEqual(result.tomorrow_kwh, 6.8)

    def test_stale_cache_is_not_exposed_as_a_number(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "forecast.json"
            config = self.config(cache)
            now = datetime(2026, 9, 22, 10, 30, tzinfo=self.zone)
            payload = {
                "hourly": {
                    "time": ["2026-09-23T10:00"],
                    "global_tilted_irradiance": [1000],
                }
            }
            client = SolarForecastClient(
                config, self.zone, lambda *_args, **_kwargs: FakeResponse(payload),
                background=False,
            )
            client.read(now)
            stale = SolarForecastClient(
                config, self.zone,
                lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
                background=False,
            ).read(now + timedelta(hours=13))
            self.assertEqual(stale.status, "stale")
            self.assertIsNone(stale.tomorrow_kwh)

    def test_missing_day_is_incomplete_not_zero_production(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {
                "hourly": {
                    "time": ["2026-09-22T10:00"],
                    "global_tilted_irradiance": [500],
                }
            }
            client = SolarForecastClient(
                self.config(Path(directory) / "forecast.json"),
                self.zone,
                lambda *_args, **_kwargs: FakeResponse(payload),
                background=False,
            )
            result = client.read(
                datetime(2026, 9, 22, 9, 0, tzinfo=self.zone)
            )
            self.assertEqual(result.status, "incomplete")
            self.assertIsNone(result.tomorrow_kwh)

    def test_background_refresh_does_not_block_the_control_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            started = threading.Event()
            release = threading.Event()

            def slow_opener(*_args, **_kwargs):
                started.set()
                release.wait(1)
                raise OSError("offline")

            client = SolarForecastClient(
                self.config(Path(directory) / "forecast.json"),
                self.zone,
                slow_opener,
            )
            result = client.read(
                datetime(2026, 9, 22, 10, 30, tzinfo=self.zone)
            )
            self.assertTrue(started.wait(0.2))
            self.assertEqual(result.status, "unavailable")
            release.set()


if __name__ == "__main__":
    unittest.main()


