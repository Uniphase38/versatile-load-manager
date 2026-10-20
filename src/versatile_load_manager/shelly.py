"""Read-only discovery and status access for Shelly Gen1 and Gen2+ devices."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ShellyStatus:
    host: str
    generation: int
    model: str
    output: Optional[bool]
    power_w: Optional[float]
    component: str = "switch"


@dataclass(frozen=True)
class ShellyCommandResult:
    host: str
    requested_output: bool
    previous_output: bool
    observed_output: bool
    command_sent: bool
    dry_run: bool


class ShellyError(IOError):
    """Raised when a Shelly cannot be identified or queried."""


class ShellyClient:
    """Probe and read a Shelly without exposing any switching operation."""

    def __init__(self, host: str, switch_id: int = 0, timeout_s: float = 3.0, session=None):
        self.host = host
        self.switch_id = switch_id
        self.timeout_s = timeout_s
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    def _gen2_rpc(self, method: str, params=None):
        response = self.session.post(
            "http://%s/rpc" % self.host,
            json={"id": 1, "method": method, "params": params or {}},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ShellyError("%s: %s" % (method, payload["error"]))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ShellyError("%s returned no result" % method)
        return result

    def _read_gen2(self) -> ShellyStatus:
        info = self._gen2_rpc("Shelly.GetDeviceInfo")
        all_status = self._gen2_rpc("Shelly.GetStatus")
        status = all_status.get("switch:%s" % self.switch_id)
        component = "switch"
        output = None
        if isinstance(status, dict):
            output = status.get("output")
            if not isinstance(output, bool):
                raise ShellyError("switch component returned no output state")
        else:
            status = all_status.get("pm1:%s" % self.switch_id)
            component = "pm1"
            if not isinstance(status, dict):
                raise ShellyError(
                    "no switch:%s or pm1:%s component found"
                    % (self.switch_id, self.switch_id)
                )
        power = status.get("apower")
        return ShellyStatus(
            host=self.host,
            generation=int(info.get("gen", 2)),
            model=str(info.get("model") or info.get("app") or "unknown"),
            output=output,
            power_w=float(power) if power is not None else None,
            component=component,
        )

    def _get_json(self, path: str):
        response = self.session.get(
            "http://%s%s" % (self.host, path), timeout=self.timeout_s
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ShellyError("%s returned invalid JSON" % path)
        return payload

    def _read_gen1(self) -> ShellyStatus:
        info = self._get_json("/shelly")
        status = self._get_json("/status")
        try:
            output = bool(status["relays"][self.switch_id]["ison"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ShellyError("Gen1 relay status is unavailable") from exc
        power = None
        meters = status.get("meters", [])
        if self.switch_id < len(meters):
            power = meters[self.switch_id].get("power")
        return ShellyStatus(
            host=self.host,
            generation=1,
            model=str(info.get("type") or info.get("model") or "unknown"),
            output=output,
            power_w=float(power) if power is not None else None,
        )

    def read_status(self) -> ShellyStatus:
        """Auto-detect the API generation and return switch status and power."""
        gen2_error = None
        try:
            return self._read_gen2()
        except (OSError, ValueError) as exc:
            gen2_error = exc
        try:
            return self._read_gen1()
        except (OSError, ValueError) as exc:
            raise ShellyError(
                "unable to query %s (Gen2+: %s; Gen1: %s)"
                % (self.host, gen2_error, exc)
            ) from exc

    def set_output(self, on: bool, dry_run: bool = True) -> ShellyCommandResult:
        """Set a relay idempotently and verify it, or only plan it in dry-run."""
        before = self.read_status()
        if before.component != "switch" or before.output is None:
            raise ShellyError(
                "%s component %s is not switchable" % (self.host, before.component)
            )
        if before.output == on:
            return ShellyCommandResult(
                self.host, on, before.output, before.output, False, dry_run
            )
        if dry_run:
            return ShellyCommandResult(
                self.host, on, before.output, before.output, False, True
            )

        if before.generation == 1:
            self._get_json(
                "/relay/%s?turn=%s" % (self.switch_id, "on" if on else "off")
            )
        else:
            self._gen2_rpc("Switch.Set", {"id": self.switch_id, "on": on})

        after = self.read_status()
        if after.component != "switch" or after.output != on:
            raise ShellyError(
                "%s did not confirm requested output %s" % (self.host, on)
            )
        return ShellyCommandResult(
            self.host, on, before.output, after.output, True, False
        )
