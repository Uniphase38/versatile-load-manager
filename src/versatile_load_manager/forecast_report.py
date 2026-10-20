"""PC-only daily forecast comparison; never connects to relays or the Cerbo."""

import argparse
import csv
from dataclasses import replace
from datetime import date, datetime, time as clock_time, timedelta, timezone
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import load_config
from .solar_forecast import SolarForecastClient


FIELDS = (
    "date", "forecast_kwh", "forecast_recorded_at", "forecast_fetched_at",
    "forecast_source", "solar_kwh", "consumption_kwh", "error_kwh",
    "error_pct", "vrm_collected_at",
)


class CsvLock:
    """Keep two collectors from overwriting each other's daily history."""

    def __init__(self, path):
        self.path = path.with_name(path.name + ".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValueError("Another collector is using this CSV") from exc
        return self

    def __exit__(self, *_args):
        self.stream.close()


def day_bounds(day, zone):
    """Calendar-day bounds, including the 23/25-hour daylight-saving days."""
    start = datetime.combine(day, clock_time(), zone)
    end = datetime.combine(day + timedelta(days=1), clock_time(), zone)
    return int(start.timestamp()), int(end.timestamp()) - 1


def daily_energy(payload):
    """Use VRM energy flows, never the misleading sum of flows called 'kwh'."""
    if payload.get("success") is not True:
        raise ValueError("VRM did not report a successful statistics request")
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("VRM returned no daily totals")

    def value(key, optional=False):
        if optional and key not in totals:
            return 0.0  # VRM omits Pg on installations without PV export.
        number = totals.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("VRM total %s is missing or invalid" % key)
        if not math.isfinite(number) or number < 0:
            raise ValueError("VRM total %s is invalid" % key)
        return float(number)

    pc, pb = value("Pc"), value("Pb")
    solar = pc + pb + value("Pg", optional=True)
    consumption = pc + value("Bc") + value("Gc")
    return solar, consumption


class VrmClient:
    """Use a VRM API access token; secrets never enter the CSV."""

    def __init__(self, constants, opener=urlopen):
        self.site_id = constants.VRM_SITE_ID
        self.api_url = constants.VRM_API_URL.rstrip("/")
        token = os.getenv("VRM_ACCESS_TOKEN") or getattr(constants, "VRM_ACCESS_TOKEN", None)
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Set VRM_ACCESS_TOKEN to a VRM API access token in the ESS .env or environment")
        self._token = token.strip()
        self._opener = opener

    def _json(self, request):
        with self._opener(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Unexpected VRM response")
        return payload

    def fetch_day(self, day, zone):
        start, end = day_bounds(day, zone)
        params = urlencode({
            "type": "kwh", "interval": "days", "start": start, "end": end,
        })
        url = "%s/installations/%s/stats?%s" % (
            self.api_url, self.site_id, params,
        )
        try:
            payload = self._json(Request(
                url, headers={"x-authorization": "Token " + self._token},
            ))
        except HTTPError as exc:
            if exc.code == 401:
                raise ValueError("VRM access token rejected (HTTP 401); check VRM_ACCESS_TOKEN and its validity in VRM") from None
            raise
        return daily_energy(payload)


def read_rows(path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError("CSV has an unexpected header: %s" % path)
        rows = {}
        for row in reader:
            date.fromisoformat(row["date"])
            if row["date"] in rows:
                raise ValueError("CSV contains a duplicate date")
            rows[row["date"]] = row
        return rows


def write_rows(path, rows):
    """Atomically replace a CSV; keep completed rows if collection fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows[key] for key in sorted(rows))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(rows, forecast, now):
    """Freeze the first valid tomorrow forecast; never invent a past forecast."""
    key = (now.date() + timedelta(days=1)).isoformat()
    existing = rows.get(key, {})
    if existing.get("forecast_kwh"):
        return False
    if forecast.status != "valid" or forecast.fetched_at is None:
        return False
    number = forecast.tomorrow_kwh
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return False
    if not math.isfinite(number) or number < 0:
        return False
    row = {field: existing.get(field, "") for field in FIELDS}
    row.update({
        "date": key, "forecast_kwh": "%.3f" % number,
        "forecast_recorded_at": now.isoformat(),
        "forecast_fetched_at": forecast.fetched_at.isoformat(),
        "forecast_source": forecast.source,
    })
    rows[key] = row
    return True


def complete_row(rows, day, solar, consumption, now):
    key = day.isoformat()
    row = {field: rows.get(key, {}).get(field, "") for field in FIELDS}
    row.update({
        "date": key, "solar_kwh": "%.3f" % solar,
        "consumption_kwh": "%.3f" % consumption,
        "vrm_collected_at": now.isoformat(),
    })
    if row["forecast_kwh"]:
        # Positive error means the forecast overestimated actual production.
        error = float(row["forecast_kwh"]) - solar
        row["error_kwh"] = "%.3f" % error
        row["error_pct"] = "%.2f" % (100 * error / solar) if solar > 0 else ""
    rows[key] = row


def tick(path, config, vrm, now, snapshot_clock, collect_clock, requested_day=None):
    rows = read_rows(path)
    problems = []
    if requested_day is None and now.time().replace(tzinfo=None) >= snapshot_clock:
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        if not rows.get(tomorrow, {}).get("forecast_kwh"):
            client = SolarForecastClient(
                replace(config.solar_forecast, cache_file=str(
                    path.with_name(path.name + ".forecast-cache.json")
                )), ZoneInfo(config.schedule.timezone),
                background=False,
            )
            forecast = client.read(now)
            if snapshot(rows, forecast, now):
                print("Forecast saved for %s: %s kWh" % (
                    tomorrow, rows[tomorrow]["forecast_kwh"],
                ), flush=True)
            else:
                problems.append("Forecast unavailable: %s" % forecast.status)

    days = set()
    if requested_day is not None:
        if requested_day >= now.date():
            raise ValueError("Only completed calendar days can be collected")
        days.add(requested_day)
    elif now.time().replace(tzinfo=None) >= collect_clock:
        # Re-read yesterday and the day before to pick up delayed VRM uploads.
        days.update(now.date() - timedelta(days=offset) for offset in (1, 2))
        days.update(
            date.fromisoformat(key) for key, row in rows.items()
            if key < now.date().isoformat() and not row.get("solar_kwh")
        )
        days = {
            day for day in days
            if not rows.get(day.isoformat(), {}).get("vrm_collected_at", "").startswith(
                now.date().isoformat()
            )
        }
    for day in sorted(days):
        try:
            solar, consumption = vrm.fetch_day(day, ZoneInfo(config.schedule.timezone))
            complete_row(rows, day, solar, consumption, now)
            print("VRM %s: solar=%.3f kWh, consumption=%.3f kWh" % (
                day, solar, consumption,
            ), flush=True)
        except (OSError, ValueError, TypeError) as exc:
            problems.append("VRM %s: %s" % (day, exc))
    write_rows(path, rows)
    for problem in problems:
        print(problem, file=sys.stderr, flush=True)
    return not problems


def parse_clock(value):
    try:
        result = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected HH:MM") from exc
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="VLM forecast configuration")
    parser.add_argument("--ess-root", type=Path, required=True, help="Local ESS project directory")
    parser.add_argument("--csv", type=Path, default=Path("data/forecast_daily.csv"))
    parser.add_argument("--snapshot-at", type=parse_clock, default=clock_time(20))
    parser.add_argument("--collect-at", type=parse_clock, default=clock_time(8))
    parser.add_argument("--day", type=date.fromisoformat, help="Collect one past day, without a forecast")
    parser.add_argument("--watch", action="store_true", help="Check schedules every five minutes")
    args = parser.parse_args(argv)
    if args.day and args.watch:
        parser.error("--day cannot be combined with --watch")
    try:
        config = load_config(args.config)
        if not config.solar_forecast.enabled and not args.day:
            raise ValueError("Enable solar_forecast in the VLM configuration")
        if not (args.ess_root / "domain" / "constants.py").is_file():
            raise ValueError("--ess-root must contain domain/constants.py")
        sys.path.insert(0, str(args.ess_root.resolve()))
        vrm = VrmClient(importlib.import_module("domain.constants"))
        zone = ZoneInfo(config.schedule.timezone)
        print("Daily comparison CSV: %s" % args.csv.resolve(), flush=True)
        with CsvLock(args.csv):
            while True:
                ok = tick(args.csv, config, vrm, datetime.now(timezone.utc).astimezone(zone),
                          args.snapshot_at, args.collect_at, args.day)
                if not args.watch:
                    return 0 if ok else 1
                time.sleep(300)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, ImportError, TypeError) as exc:
        print("Daily comparison failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
