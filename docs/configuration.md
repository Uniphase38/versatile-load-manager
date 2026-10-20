# Configuration reference

For worked examples showing how these values combine into real decisions, see
[Understanding VLM decisions](understanding-vlm-decisions.md).

The installed configuration is `/data/versatile-load-manager/config.yaml`. The
repository example is `config/config.example.yaml`. `install.sh` creates the
file only when it does not already exist.

After editing, validate by running one diagnostic command or one dry-run cycle
before enabling real commands:

```sh
PYTHONPATH=/data/versatile-load-manager/vendor:/data/versatile-load-manager/src \
  python3 -m versatile_load_manager diagnose \
  --config /data/versatile-load-manager/config.yaml

PYTHONPATH=/data/versatile-load-manager/vendor:/data/versatile-load-manager/src \
  python3 -m versatile_load_manager run \
  --config /data/versatile-load-manager/config.yaml --once
```

## Independent control locks

Real Shelly commands require all three conditions:

```yaml
dry_run: false
control_enabled: true
managed_loads:
  - ExactLoadName
```

DVCC writes use a separate lock and require:

```yaml
dry_run: false
battery:
  control_enabled: true
```

`dry_run: true` therefore blocks both kinds of writes. Keep both control locks
false during first commissioning.

## `cerbo`

| Key | Meaning |
| --- | --- |
| `host` | GX hostname or address; use `localhost` when VLM runs on the GX device |
| `port` | Modbus TCP port, normally `502` |
| `timeout_s` | Per-operation communication timeout |
| `unit_id_scan_start`, `unit_id_scan_end` | Inclusive discovery range, between 0 and 247 |
| `service_cache_file` | Persistent cache for discovered system and VE.Bus unit IDs |
| `system_unit_id`, `vebus_unit_id` | `auto` for discovery, or an explicit diagnostic override |

Automatic discovery is preferred. A stale cache is checked against the live
services and replaced when necessary.

## `watchdog`

| Key | Default | Meaning |
| --- | ---: | --- |
| `safe_after_errors` | `3` | Consecutive critical Cerbo failures before SAFE mode stops VLM-owned loads and attempts battery restoration |
| `recovery_cycles` | `2` | Complete healthy telemetry cycles required before starts resume, including after process startup |
| `restart_after_errors` | `10` | Consecutive failures before VLM exits so runit recreates the process and Modbus connection |

`restart_after_errors` must be greater than or equal to
`safe_after_errors`. All three values are positive integers. SAFE restoration
is best-effort: if Modbus remains unavailable, relay safety still runs through
the Shelly network and the failed battery restoration is logged for the next
attempt.

## `battery`

| Key | Meaning |
| --- | --- |
| `soc_min` | Lower reserve used by solar-window load policy |
| `soc_max` | Normal charging target; VLM requests 0 A when reached |
| `soc_on` | SOC above which confirmed curtailed surplus may start loads |
| `soc_off` | Solar-window stop threshold for running loads |
| `load_start_hysteresis_soc` | Margin above `soc_min` for starts based on visible charging surplus |
| `max_charge_current_a` | Installation-wide safe DVCC ceiling |
| `charge_hysteresis_soc` | SOC fall required before charging reopens below `soc_max` |
| `control_enabled` | Independent permission for real DVCC writes |
| `balance_target_soc` | Top-balancing target, greater than `soc_max` and at most 100 |
| `balance_interval_days` | Interval between completed balancing cycles |
| `balance_hold_s` | Continuous time required at the balancing target |
| `balance_reference_date` | Optional `YYYY-MM-DD` bootstrap date used only until history is persisted |
| `balance_advance_days` | Opt-in anticipation window in calendar days before the due date; default `0`, suggested `3` |
| `balance_solar_deadline` | Local deadline for reaching the target during an anticipated attempt; default `16:00` |
| `balance_good_solar_day_kwh` | Minimum valid forecast for today's total PV energy to select an early attempt; default `15` kWh |

With `balance_advance_days: 3`, a balance due on 18 October (even at 21:23)
can begin from 15 October at `schedule.solar_start`. A good current forecast
and confirmed live solar charging or curtailment select the attempt, without
waiting for SOC to reach `soc_max`. All managed solar loads are deferred until
the continuous balance hold finishes, including during transient deficits,
unless monitored surplus sharing below admits a load trial.
Already-running owned loads stop after their minimum ON time; externally
controlled relays retain their normal ownership protection.

If the target is not reached by `balance_solar_deadline`, VLM restores the
normal ceiling and releases loads to their usual policies. An already-started
hold may finish after that deadline, but never outside the solar window; if
SOC drops below target after the deadline, the attempt is abandoned. The
deadline must lie after `solar_start` and at/before `solar_end` in a daytime
window. No new anticipated attempt starts after it.

Attempt priority survives a restart on the same day. A new day must qualify
again; failure does not move the due date, and only successful completion
starts a new interval. Solar anticipation never raises ESS minimum SOC or
authorizes early grid catch-up. HC catch-up keeps its independent enable and
overdue/defer rules. `battery.control_enabled: true` and `dry_run: false` are
required for real battery control; a valid enabled forecast is needed to
select an anticipated day. Missing/stale forecasts cannot select a new attempt.

`max_charge_current_a` protects shared cables, fuses and busbars. It must be
chosen from the weakest installation component, not merely from the sum of
battery module capabilities.

### Optional balancing load allowances

These keys belong under `battery`. Both permissions default to false:

| Key | Default | Meaning |
| --- | --- | --- |
| `balance_surplus_enabled` | `false` | Allow monitored trials on confirmed curtailment during solar charging and holding |
| `balance_surplus_charge_tolerance_w` | `150` | Permitted fall in battery charge power from its observed pre-trial reference |
| `post_balance_support_enabled` | `false` | Finish unmet goals with bounded battery support on the day of an actual success |
| `post_balance_min_soc` | `90` | Stop support at/below this SOC; starts also require the existing load-start SOC margin |
| `post_balance_max_discharge_w` | `3000` | Aggregate measured battery discharge limit, not a per-load allowance |
| `post_balance_extension_s` | `10800` | Extension after `solar_end`, clamped to the start of HC |

Curtailment does not quantify recoverable watts. Surplus sharing therefore
starts one load at a time through existing AC headroom, stagger, active-power
probe and cooldown guards. It records actual battery charge power before the
trial. A running load can remain after curtailment disappears only while
battery charge is preserved within tolerance and grid import stays within
`grid_import_tolerance_w`. A deficit or missing flow telemetry withdraws the
allowance. This protection may interrupt minimum ON time. The charge reference
is renewed only when curtailment is confirmed after settling, allowing natural
BMS taper during 97--100% charge and the hold. A restart requires fresh
curtailment qualification; no unknown reference is assumed. Small transients
within tolerance remain possible during a trial. HC balancing is unaffected.

After a real completed hold, support applies from the normal solar start until
the earlier of `solar_end + post_balance_extension_s` and HC. With 18:00 solar
end and a three-hour extension, the deadline is 21:00. It also tolerates bounded
battery discharge before 18:00, so a small `energy_deficit` no longer stops the
loads by itself. SOC, grid-import and power telemetry guards still apply;
deadline and SOC floor take precedence over minimum ON time. Power shedding
removes the lowest-priority owned loads first. Normal start headroom and
minimum ON/OFF guards remain active.

Runtime loads stop at their desired target, or their required runtime if no
target exists. Thermostat loads stop when satisfied. Completed loads are never
rearmed by this allowance. This is not a general full-battery policy: a
persisted `last_success_at` is required, survives restart and expires with the
calendar day. Old runtime files without this marker do not get an inferred
success from their bootstrap/reference date. Both allowances require real
battery-control permission; external relay ownership and hard safety stops
remain unchanged. Support does not raise ESS minimum SOC, and it never replaces
the existing required-runtime HC catch-up.

## `schedule`

All clocks use `HH:MM`. `timezone` must be an IANA name such as
`Europe/Paris`; daylight-saving changes are automatic.

| Key | Meaning |
| --- | --- |
| `solar_start`, `solar_end` | Window in which solar load policy operates |
| `off_peak_start` | Beginning of required-runtime catch-up |
| `daily_reset` | End of off-peak catch-up and start of a new VLM runtime day |

The month used by a seasonal target is the month of the VLM day. It changes at
the daily reset, not at midnight.

## `tempo`

This optional provider retrieves and caches the current and next EDF Tempo day
colours. It is observation-only: the values are logged but cannot change SOC
targets, DVCC or load commands.

| Key | Meaning |
| --- | --- |
| `enabled` | Enable Tempo retrieval, caching and change-based logging |
| `provider` | Tempo source; currently `api-couleur-tempo.fr` |
| `cache_file` | Dedicated atomic cache; must not be the runtime state file |
| `refresh_interval_s` | Successful refresh cadence |
| `retry_interval_s` | Retry delay after a provider failure |
| `fresh_for_s` | Maximum age reported as fresh before cached data becomes stale |
| `timeout_s` | Network timeout used only by the background worker |

## `solar_forecast`

This optional provider retrieves Open-Meteo global
tilted irradiance (GTI), converts the hourly values to approximate production
for the complete PV installation and logs today's total, today's remaining
energy and tomorrow's total. When solar anticipation is enabled, today's valid
forecast helps select a balance attempt; the battery/load policies then raise
the ceiling and defer loads. Live solar evidence is also required. The HC
planner uses tomorrow's forecast under its separate catch-up rules.

| Key | Meaning |
| --- | --- |
| `enabled` | Enable background retrieval and forecast logging |
| `latitude`, `longitude` | Installation coordinates in decimal degrees |
| `peak_power_kwp` | Effective total installed PV power used by the estimate |
| `tilt_deg` | Panel tilt: 0° horizontal to 90° vertical |
| `azimuth_deg` | Geographic compass azimuth: 0° north, 90° east, 180° south, 270° west |
| `performance_ratio` | Fixed conversion/loss coefficient; initially `0.85` |
| `refresh_interval_s` | Successful refresh cadence; default 3 hours |
| `retry_interval_s` | Retry delay after a provider failure; default 15 minutes |
| `max_age_s` | Maximum cache age accepted for numeric output; default 12 hours |
| `timeout_s` | Network timeout used only by the background worker |
| `cache_file` | Dedicated atomic cache; must not be the runtime state file |

An enabled provider requires every site and array field. Network retrieval runs
in a daemon thread and therefore cannot delay the H24 control loop. A malformed,
incomplete or stale forecast is logged without numeric production; VLM never
assumes future solar from unusable remote data.

## Global policy

| Key | Meaning |
| --- | --- |
| `loop_interval_s` | Delay between policy cycles |
| `start_settle_s` | Global delay after a confirmed load start or DVCC transition |
| `status_log_interval_s` | Full status heartbeat interval; changes and errors remain immediate |
| `max_ac_power_w` | Maximum permitted measured installation AC power |
| `ac_start_margin_w` | Additional start-only reserve below the maximum |
| `surplus_confirm_cycles` | Consecutive cycles required to confirm PV surplus |
| `deficit_confirm_cycles` | Consecutive cycles required to confirm energy deficit |
| `grid_import_tolerance_w` | Import ignored as measurement noise |
| `battery_discharge_tolerance_w` | Discharge ignored as measurement noise |
| `battery_charge_reserve_w` | Visible surplus reserved for continued battery charging |
| `state_file` | Persistent runtime JSON path |

A start is admitted only when:

```text
measured AC + startup power <= max AC - start margin
```

Running-load shedding uses `max_ac_power_w` itself, without the start margin.

## Load fields

### Identity and priority

| Key | Meaning |
| --- | --- |
| `name` | Unique identifier; must exactly match `managed_loads` |
| `host` | Shelly hostname or address |
| `switch_id` | Shelly output index, default `0` |
| `enabled` | Policy inclusion without deleting the definition |
| `priority` | Lower number starts first; overload shedding runs in reverse order |
| `rated_w` | Expected steady power used for allocation and projection |
| `startup_power_w` | Optional higher start-only AC budget; cannot be below `rated_w` |

### Runtime and completion

| Key | Meaning |
| --- | --- |
| `min_daily_runtime_h` | Fixed daily required runtime when no desired target is configured |
| `completion_mode` | `runtime` or `thermostat` |
| `target_runtime_h` | Optional fixed desired runtime |
| `target_runtime_h_by_month` | Desired runtime map containing every integer month 1 through 12 |
| `required_runtime_ratio` | Fraction from 0 to 1 guaranteed by off-peak catch-up |

`target_runtime_h` and `target_runtime_h_by_month` are mutually exclusive. A
target of 6 h with a ratio of 0.5 produces a 3 h required runtime and a 6 h
desired runtime.

### Active-power probe

| Key | Meaning |
| --- | --- |
| `min_active_power_w` | Active threshold; default is 50% of `rated_w` |
| `power_confirm_timeout_s` | Maximum wait after a commanded start |
| `probe_retry_delay_s` | Delay before retry in cooldown mode |
| `probe_failure_mode` | `cooldown` for thermostatic/inactive loads, `lockout` for motor faults |

### Anti-cycling

| Key | Meaning |
| --- | --- |
| `min_on_time_s` | Minimum healthy ON duration protected from ordinary energy fluctuations |
| `min_off_time_s` | Delay before a new start after an observed OFF transition |

Grid loss, invalid telemetry, overload and active-power faults remain hard
safety conditions. A start is also refused when the allowed window cannot
contain `min_on_time_s`.

### Target finishing and best-effort progress

| Key | Meaning |
| --- | --- |
| `target_finish_enabled` | Enables both finishing and final best-effort progress |
| `target_finish_max_remaining_h` | Maximum assisted finishing duration and length of the final progress interval |
| `target_finish_min_soc` | Minimum SOC for either assistance mode |
| `target_finish_max_battery_discharge_w` | Maximum permitted battery support |
| `target_finish_grace_s` | Extension after `solar_end`; `0` disables the extension |

These modes are considered only after required runtime is complete:

- `target_finish`: remaining desired runtime is no greater than
  `target_finish_max_remaining_h`;
- `target_progress`: more time remains than can be finished, so VLM waits for
  the last `target_finish_max_remaining_h` before the grace deadline and makes
  as much progress as safely possible.

For `solar_end: 18:00`, a one-hour grace and a one-hour assistance duration,
best-effort progress begins no earlier than 18:00 and ends no later than 19:00.

## Applying changes

VLM reads YAML at process startup. After a deliberate configuration change:

```sh
svc -t /service/versatile-load-manager
tail -f /data/log/versatile-load-manager/current
```

Do not delete `runtime.json` merely to reload configuration. It contains daily
runtimes, ownership, lockouts and balancing history.

