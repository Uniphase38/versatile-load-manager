import io
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from versatile_load_manager.tempo import (ApiCouleurTempoProvider, TempoCache, TempoColour,
                   TempoError, TempoObserver, cache_status)


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def opener_for(*payloads):
    remaining = list(payloads)
    def open_(_request, timeout):
        assert timeout == 2
        return Response(json.dumps(remaining.pop(0)).encode())
    return open_


def test_provider_normalizes_today_and_tomorrow():
    provider = ApiCouleurTempoProvider(timeout_s=2, opener=opener_for(
        {"dateJour": "2026-09-22", "codeJour": 1},
        {"dateJour": "2026-09-23", "codeJour": 3},
    ))
    now = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)
    value = provider.fetch(date(2026, 9, 22), now)
    assert value.today.colour is TempoColour.BLUE
    assert value.tomorrow.colour is TempoColour.RED
    assert value.observed_at == now


def test_provider_rejects_wrong_date_and_unknown_code():
    provider = ApiCouleurTempoProvider(timeout_s=2, opener=opener_for(
        {"dateJour": "2026-09-21", "codeJour": 1}))
    with pytest.raises(TempoError, match="does not match"):
        provider.fetch(date(2026, 9, 22))
    provider = ApiCouleurTempoProvider(timeout_s=2, opener=opener_for(
        {"dateJour": "2026-09-22", "codeJour": 9}))
    with pytest.raises(TempoError, match="unknown codeJour"):
        provider.fetch(date(2026, 9, 22))


def test_cache_round_trip_and_validity(tmp_path):
    provider = ApiCouleurTempoProvider(timeout_s=2, opener=opener_for(
        {"dateJour": "2026-09-22", "codeJour": 2},
        {"dateJour": "2026-09-23", "codeJour": 0}))
    observed = datetime(2026, 9, 22, 9, tzinfo=timezone.utc)
    original = provider.fetch(date(2026, 9, 22), observed)
    cache = TempoCache(tmp_path / "tempo.json")
    cache.save(original)
    loaded = cache.load()
    assert loaded == original
    assert cache_status(loaded, date(2026, 9, 22), observed + timedelta(hours=1), 7200) == "fresh"
    assert cache_status(loaded, date(2026, 9, 22), observed + timedelta(hours=3), 7200) == "stale"
    assert cache_status(loaded, date(2026, 9, 23), observed, 7200) == "invalid"
    assert cache_status(None, date(2026, 9, 22), observed, 7200) == "missing"


def test_observer_refreshes_then_uses_cache_on_provider_failure(tmp_path):
    now = datetime(2026, 9, 22, 10, tzinfo=timezone.utc)
    provider = ApiCouleurTempoProvider(timeout_s=2, opener=opener_for(
        {"dateJour": "2026-09-22", "codeJour": 1},
        {"dateJour": "2026-09-23", "codeJour": 2},
    ))
    cache = TempoCache(tmp_path / "tempo.json")
    observer = TempoObserver(provider, cache, 3600, 300, 7200)

    first = observer.poll(now)
    assert first.refreshed
    assert first.status == "fresh"
    assert first.error is None

    class FailingProvider:
        def fetch(self, *_args):
            raise TempoError("network unavailable")

    restarted = TempoObserver(FailingProvider(), cache, 3600, 300, 7200)
    fallback = restarted.poll(now + timedelta(minutes=30))
    assert not fallback.refreshed
    assert fallback.status == "fresh"
    assert fallback.snapshot == first.snapshot
    assert fallback.error == "network unavailable"


def test_observer_reports_missing_without_cache(tmp_path):
    class FailingProvider:
        def fetch(self, *_args):
            raise TempoError("offline")

    observer = TempoObserver(
        FailingProvider(), TempoCache(tmp_path / "missing.json"), 3600, 300, 7200
    )
    value = observer.poll(datetime(2026, 9, 22, 10, tzinfo=timezone.utc))
    assert value.snapshot is None
    assert value.status == "missing"
    assert value.error == "offline"
