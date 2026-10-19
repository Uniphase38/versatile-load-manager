# VLM behavior reference

Independent H24 controller for selected flexible loads, using local energy data
from a Victron Venus OS installation and Shelly relays. Relay control is
protected by two independent configuration locks: `control_enabled` and
`dry_run`.

This is an independent community project. It is not an official Victron Energy
or Shelly product and is not affiliated with or endorsed by either company.
Victron, Venus OS and Shelly are trademarks of their respective owners.

The existing technical identifiers (`victron-load-manager`, service paths and
archive names) are retained for compatibility with installed systems.

This document preserves the detailed behavior and safety notes that previously lived in the project README. For the project overview and first installation path, start with the [README](../README.md).

## Documentation

- [Understanding VLM decisions](docs/understanding-vlm-decisions.md)
- [Architecture and safety model](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Troubleshooting and log collection](docs/troubleshooting.md)
- [Tempo and solar-forecast design](docs/tempo-design.md)
- [Daily forecast comparison with VRM](docs/forecast-daily-report.md)
- [Reference installation: Fronius zero-export fallback](docs/fronius-zero-export-fallback.md)

## Venus OS installation

Solar balancing can optionally start up to three days early on a favorable
day, reserving the solar window for the battery before running loads. Set
`battery.balance_advance_days: 3` and review `balance_solar_deadline` (default
16:00), with battery control and PV forecasts enabled. See the
[configuration reference](docs/configuration.md) for qualifications, deadline
recovery and restart behavior. Existing installations keep their policy until
this option is explicitly enabled.

Two further battery options are opt-in: `balance_surplus_enabled` admits
monitored trials to recover curtailed PV during charging/holding, and
`post_balance_support_enabled` lets unmet loads finish after a successful
balance with configured SOC, battery-power and time limits. They do not rearm
completed loads or force grid recharge; see the configuration reference.

For a first installation, start with the
[Cerbo getting-started guide](docs/getting-started-cerbo.md). It explains the
required installation facts in plain language and keeps all commands disabled
until the operator deliberately enables them.

The installer creates `/data/victron-load-manager`, installs a native supervised
service, and leaves it stopped. It never overwrites an existing `config.yaml`.

When dependencies have been staged in `vendor/`:

```sh
chmod +x install.sh
./install.sh
```

For an existing external vendor directory:

```sh
VLM_VENDOR_SOURCE=/data/victron-load-manager-test/vendor ./install.sh
```

Review `/data/victron-load-manager/config.yaml` and keep
`control_enabled: false` for the first H24 observation. Then start and inspect:

```sh
/data/vlm enable
tail -f /data/victron-load-manager/logs/current
```

Stop temporarily (automatic start remains enabled for the next reboot) with:

```sh
svc -d /service/victron-load-manager
```

Stop persistently with:

```sh
/data/vlm disable
```

`/data/vlm enable` removes the runit `down` marker and starts the service. Together
with the service-link entry installed in `/data/rc.local`, this makes VLM start
automatically after a Cerbo reboot.

## Safety lockouts

A load configured with `probe_failure_mode: lockout` stays locked when it draws
no active power after a start command. This is intended for motors such as a
pool pump, where an energized relay with no measured load indicates a fault.
A thermostatically satisfied water heater uses `thermostat_satisfied` instead
and is not treated as faulty.

The installer adds the short administrative command `vlm`:

```sh
/data/vlm faults
/data/vlm unlock PoolPump
```

`/data/vlm faults` is read-only. `/data/vlm unlock` asks for confirmation, pauses the
supervised service if it is running, clears only the selected lockout, then
restarts the service. The daily runtime counters are preserved.

Run `vlm --help` to display the available commands.

## Local time

Schedules are interpreted in the explicit IANA timezone configured here,
independently of the GX device system timezone:

```yaml
schedule:
  timezone: Europe/Paris
  solar_start: "10:00"
  solar_end: "18:00"
  off_peak_start: "22:00"
  daily_reset: "06:00"
```

Daylight-saving changes are applied automatically. Continuous controller logs
use the same local timezone and include the UTC offset.

## Off-peak catch-up and battery reserve

An unfinished required runtime remains mandatory throughout the off-peak
window. VLM starts or keeps that load on even when SOC is at or below
`battery.soc_min`, and a detected energy deficit does not cancel it. Native ESS
remains responsible for preserving its configured minimum SOC: once that
reserve is reached, the grid supplies the required load instead of the battery.

Grid loss, invalid telemetry and AC overload still stop the load immediately.
The fixed reserve can later be supplied by a tariff-aware policy (for example
French Tempo) without changing this off-peak rule.

Solar-window starts based on visible battery-charging surplus require
`soc_min + load_start_hysteresis_soc`. With the default one-percent margin, a
load cannot start at the reserve boundary and immediately stop on the next SOC
sample. This hysteresis does not apply to mandatory off-peak catch-up.

## Load anti-cycling

Minimum ON and OFF times can be configured per load. The sample water heaters
use two minutes in each state to avoid rapid relay cycling. The sample pool
pump cannot be stopped by a temporary energy deficit during its first 15
minutes, and cannot restart during the 5 minutes following a stop:

```yaml
min_on_time_s: 900
min_off_time_s: 300
```

These delays survive a controller restart. Hard safety conditions still stop
the load immediately: grid loss, invalid telemetry, AC overload, low battery
SOC, missing active-power telemetry, or a motor drawing no power. A water
heater whose thermostat opens is also stopped immediately and marked complete;
`min_on_time_s` never keeps an inactive heater relay energized. A load that is
already OFF when first managed is immediately eligible; the controller does
not invent an initial OFF delay.

For a motor with a measured startup peak, reserve that short peak separately
from its steady consumption:

```yaml
rated_w: 1300
startup_power_w: 3000
```

`startup_power_w` is used only by the AC headroom admission check (and the
guarded commissioning check). Surplus allocation and running-load accounting
continue to use `rated_w`. When omitted, it defaults to `rated_w`.

Successive starts are separated by `start_settle_s` (30 seconds by default),
independently of the control-loop interval. A new start is also refused when
less than the load's `min_on_time_s` remains in the current solar or off-peak
window.

## Process and relay ownership

The `run` command holds `/var/run/victron-load-manager.lock` for its full
lifetime. A second manual or supervised launch exits before opening Modbus or
contacting a Shelly. Startup logs include the package version and the absolute
source path so a copied test tree is immediately distinguishable from the
installed service.

VLM persists the names of relays it commanded ON. On shutdown it switches OFF
only those still-owned relays, making repeated shutdown cleanup harmless and
leaving an externally switched relay untouched. Ownership survives a Cerbo
restart. An externally observed ON relay is logged and adopted only at the
next daily reset; grid loss, invalid telemetry, AC overload and active-power
faults may still force it OFF immediately.

## Cerbo/Modbus watchdog

VLM starts in recovery mode and requires two complete healthy telemetry cycles
before authorising a load start. Critical Cerbo failures are counted
consecutively: new starts are blocked immediately, SAFE mode is entered after
three failures by default, and owned loads are then switched off. When battery
control is enabled, VLM also attempts to restore its captured ESS `Min_SOC` and
the configured fail-open DVCC ceiling.

After ten consecutive failures by default, VLM exits with a dedicated status
so runit recreates the process and Modbus connection. Incident evidence is
preserved in `runtime.json`. An externally switched relay is not claimed by
this communication safeguard; the separate `GRID_LOST` hard safety may still
switch it off.

## Load completion

Select the completion rule per load:

```yaml
# Water heater: continue in an allowed energy window until measured power
# falls to zero because the thermostat has opened.
completion_mode: thermostat

# Motor or timed appliance: stop when its runtime objective is complete.
completion_mode: runtime
```

Once a thermostatic load stops drawing power, its daily cycle is persisted as
complete and it is not probed again before the daily reset. Thermostat
completion never overrides hard safety, the solar deficit guard, or the
configured solar/off-peak windows. `runtime` remains the default for backward
compatibility.

A runtime load may distinguish its desired solar target from the duration that
must be guaranteed using off-peak catch-up:

```yaml
target_runtime_h: 8
required_runtime_ratio: 0.5
```

Here the load stops after eight accumulated hours, while off-peak operation is
used only when needed to guarantee four hours. Existing
`min_daily_runtime_h` configurations retain their previous meaning.

Near the desired target, a runtime load can optionally finish through a short
solar fluctuation instead of abandoning the last few minutes:

```yaml
target_finish_enabled: true
target_finish_max_remaining_h: 1
target_finish_min_soc: 80
target_finish_max_battery_discharge_w: 500
target_finish_grace_s: 3600
```

This mode is considered only after the required runtime is already complete.
It may use at most the configured battery discharge and may continue for the
configured grace after `solar_end`. A start either reserves `rated_w` against
the measured battery flow or requires confirmed PV curtailment without an
energy deficit; unknown battery power without confirmed curtailment is treated
as unsafe.
The load's `min_on_time_s` applies to a finishing start, while grid loss,
invalid telemetry, AC overload and battery balancing remain authoritative.

When more runtime remains than can be completed, the same configuration enables
`target_progress`. VLM waits until only `target_finish_max_remaining_h` remains
before the end of the grace period, then makes bounded best-effort progress
under the same SOC, battery-discharge, AC and minimum-ON guards. There is no
separate `target_progress` configuration key.

During an active balancing charge, loads may still use measured solar surplus
only after preserving `battery_charge_reserve_w` for the battery. During the
100% hold, confirmed PV curtailment may also feed one load at a time; a
confirmed energy deficit blocks or stops it after `min_on_time_s`.
The battery status line reports both `held=HH:MM:SS` and
`remaining=HH:MM:SS` while that hold is in progress.

The desired target may instead follow a generic per-load monthly schedule:

```yaml
target_runtime_h_by_month:
  1: 2
  2: 2
  3: 4
  4: 6
  5: 7
  6: 8
  7: 8
  8: 8
  9: 6
  10: 4
  11: 2
  12: 2
required_runtime_ratio: 0.5
```

This option is available to every runtime load, not only a pool pump. All 12
numeric month keys are required. A fixed `target_runtime_h` and a monthly map
are mutually exclusive. The selected month changes at `daily_reset`, keeping
one VLM runtime day on a single target even when midnight crosses a month.

The control loop remains fast, while `status_log_interval_s` controls the full
telemetry heartbeat. Commands, errors, reset events and decision changes are
always logged immediately.

## Solar-only battery balancing

Battery DVCC control has its own lock and remains observational by default,
independently of real Shelly control:

```yaml
battery:
  control_enabled: false
  soc_max: 90
  max_charge_current_a: 100
  charge_hysteresis_soc: 1
  balance_target_soc: 100
  balance_interval_days: 30
  balance_hold_s: 3600
  # Optional bootstrap from an existing controller's last successful cycle.
  # balance_reference_date: "2026-08-18"
```

The normal controller is deliberately binary: below 90%, it writes the
installation-safe `max_charge_current_a` ceiling to DVCC register 2705. At 90%
it writes 0 A, and it does not allow charging again until SOC falls to 89%.
During balancing the hysteresis is suspended: VLM keeps the protected ceiling
open until the battery has reported 100% continuously for the configured hold
time. The Pylontech BMS remains responsible for alternating or reducing the
actual current while its cells equalise.

This user ceiling protects shared wiring and over-current protection, not only
the battery modules. On the reference installation, the standard Pylontech
battery leads and 125 A fuse require keeping the existing 100 A DVCC limit even
though six US5000 modules could collectively accept more current. Never derive
this setting solely from the aggregate battery capability.

When a cycle is due, VLM waits until the normal SOC ceiling has been reached
and for confirmed PV curtailment inside the solar window before raising the
charge ceiling. It never raises ESS `Min_SOC`, so the
balancing campaign does not deliberately request grid charging. A persistent
energy deficit, the end of the solar window, or a new day without fresh surplus
evidence pauses the campaign at the normal SOC ceiling. The campaign resumes on
the next suitable sunny period and completes only after 100% has been held
continuously for `balance_hold_s`. VLM then returns to the normal 90% ceiling,
which immediately requests 0 A at that SOC.

While balancing actually uses the elevated target, solar flexible loads are
kept off so the battery has priority. Motor minimum-ON protection is still
honoured. Balancing history survives the daily runtime reset in `runtime.json`.

DVCC is written only when both `battery.control_enabled: true` and global
`dry_run: false` are set. On controller shutdown, an enabled battery controller
restores `max_charge_current_a` as a fail-open safety value.
