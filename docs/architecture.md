# Architecture

## Scope

Versatile Load Manager (VLM) is a standalone H24 controller for selected Shelly
loads on a Victron Venus OS system. It reads the installation through Modbus
TCP, evaluates one deterministic policy cycle, then optionally commands Shelly
outputs and the DVCC maximum charge current.

For an operator-focused explanation with field examples, see
[Understanding VLM decisions](understanding-vlm-decisions.md).

VLM does not replace the Victron ESS safety mechanisms or the BMS. ESS keeps
the battery reserve, the BMS remains authoritative for admissible battery
current, and each downstream protection device remains authoritative for its
own circuit.

## Runtime components

| Component | Responsibility |
| --- | --- |
| `main.py` | CLI, process lock, H24 loop, signal handling and status logging |
| `modbus.py` | Victron service discovery, telemetry and DVCC access |
| `shelly.py` | Shelly Gen1 relay and Gen2+ switch API compatibility |
| `controller.py` | State persistence, flow qualification, ownership and command execution |
| `policy.py` | Pure per-cycle load decisions and reason codes |
| `battery.py` | Normal SOC ceiling and periodic solar-only balancing policy |
| `config.py` | YAML parsing, defaults and consistency validation |

The supervised Venus OS service runs from `/data/versatile-load-manager`. Only
one `run` process may hold `/var/run/versatile-load-manager.lock` at a time.

## Shelly compatibility

The client contains two unauthenticated local-API paths:

- Gen1: `/shelly`, `/status` and `/relay/<id>?turn=...`;
- Gen2 and later: `/rpc` with `Shelly.GetDeviceInfo`, `Shelly.GetStatus` and
  `Switch.Set`.

Gen2/Gen3 `pm1` components can be identified and read as meters, but are
deliberately rejected as switchable loads. A managed load also needs usable
active-power telemetry for start confirmation and continued safe operation.

The automated tests cover Gen1 status fallback and the Gen2+ command path. Live
commissioning has so far validated the Gen2 `SNSW-001P16EU` and
`SPSW-201PE16EU` devices. Gen1 switching is therefore
implemented but not yet proven on physical hardware, and authenticated Shelly
APIs are not currently supported.

## Control cycle

Each cycle follows the same order:

1. Read all configured Shelly outputs and active power.
2. Update observed output transitions, runtimes, completion and ownership.
3. Resolve the Victron system and VE.Bus services, using the persistent cache
   when it remains valid.
4. Read SOC, AC power, grid state, grid flow, battery flow, PV limiting and
   the current DVCC limit.
5. Debounce surplus and deficit evidence across the configured number of
   cycles.
6. Evaluate battery policy and load policy.
7. Apply commands only when their independent safety locks permit it.
8. Atomically persist the new runtime state and log decisions.

New load starts are serialized. A cycle schedules at most one start and
`start_settle_s` blocks later starts while power flows settle.

## Decision precedence

The important policy ordering is:

1. Invalid or unreachable device state is never guessed.
2. Grid loss and invalid Victron telemetry stop managed active loads.
3. AC overload sheds active loads from lowest to highest priority until the
   projected power is back below `max_ac_power_w`.
4. Disabled loads are not started, and persistent motor lockouts are forced
   off.
5. Missing or inactive measured power stops an energized load.
6. `min_on_time_s` protects a healthy running load from ordinary fluctuations.
7. Runtime, solar, balancing, off-peak and finishing rules decide normal use.
8. `min_off_time_s`, window duration, start staggering and AC start headroom
   gate every new start.

Hard safety reasons may override ownership and minimum-ON protection where the
implementation explicitly identifies an unsafe state.

## Solar qualification

VLM does not infer surplus from SOC alone. It combines native PV-limiting
evidence with grid and battery power:

- meaningful grid import or battery discharge is a deficit;
- persistent curtailment with no deficit is confirmed surplus;
- visible battery-charging surplus may start a load before curtailment, while
  preserving `battery_charge_reserve_w` for continued charging;
- surplus and deficit must persist for their configured confirmation cycles.

A DVCC transition resets the flow qualification and starts a settling delay.
This prevents a load start from using stale measurements taken just before the
battery command changed.

### Native PV-limiting detection

VLM runs on the GX device and reads the detailed Venus OS D-Bus services
directly. AC-coupled and DC-coupled PV require different evidence:

| Source | D-Bus values | Interpretation |
| --- | --- | --- |
| Victron solar charger | `/MppOperationMode` and `/Yield/Power` on each `com.victronenergy.solarcharger.*` service | Mode `1` confirms that the charger is power-limited. Mode `2` means normal MPP tracking. Any other or missing mode, including a stopped charger, is unknown rather than proof of limiting. Power is logged for diagnosis but does not decide the state. |
| AC-coupled inverter, including Fronius | `/Ac/Power`, `/Ac/PowerLimit` and `/Ac/MaxPower` on each `com.victronenergy.pvinverter.*` service | A limit is requested when `PowerLimit < MaxPower - 1 W`. Limiting is confirmed only when the positive measured output has settled close to that limit: within the greater of `100 W` or `5 %` of the requested limit. |
| ESS context | `/PvPowerLimiterActive` from the Hub-4/system service | Logged as `ESS=active` or `inactive`. It is useful context, but is not by itself accepted as proof that usable curtailed PV exists. |

The AC distinction between **request** and **confirmation** is deliberate. A
Fronius may have a low power cap while a cloud, low irradiance or sunset keeps
its actual output well below that cap. In that case VLM logs
`AC_request=active` but `AC_limit=unconfirmed` and does not claim curtailed
energy. A zero limit or output below `50 W` is also treated as unknown, which
prevents a night-time `0 W / 0 W` condition from being mistaken for surplus.

For DC PV, one charger reporting mode `1` is sufficient to confirm DC
curtailment. Mode `2` explicitly reports normal MPPT tracking. A stopped
charger, an unsupported path or an unreadable value remains `unknown`; VLM
does not guess from low power alone.

The status line exposes the raw evidence and the resulting interpretation. For
example:

```text
PV limiting: ESS=active, DC=limited [charger_1:mode=1,power=600W], AC_request=active, AC_limit=confirmed [inverter_1:power=800W,limit=800W,max=5000W]
```

Here both the Victron charger and the Fronius are confirmed limited. By
contrast, `DC=unknown [charger_1:mode=0,power=0W]` does not mean that the DC charger
is curtailed; it means that no usable conclusion can be drawn from its current
operating mode.

Confirmed native curtailment is still not enough to start a load. VLM also
checks the simultaneous grid and battery flows. With the log convention used
by VLM, positive battery power is charging and negative grid power is export,
so the visible balance is:

```text
visible_surplus_w = battery_power_w - grid_power_w
```

The result must show no meaningful energy deficit for
`surplus_confirm_cycles` consecutive cycles. The tolerated band is the sum of
`grid_import_tolerance_w` and `battery_discharge_tolerance_w`. This persistence
and flow check prevents an asynchronous D-Bus sample or a brief setpoint
transition from authorising a load start.

Detailed per-device detection is available only when VLM runs locally on the
GX device. A remote Modbus connection exposes the aggregate
`PvPowerLimiterActive` flag, but not enough evidence for VLM to confirm AC or
DC curtailment safely.

## Runtime objectives

Runtime is accumulated from an observed ON output that is drawing active
power. A load may have:

- a fixed minimum runtime through `min_daily_runtime_h`;
- a desired fixed or monthly target;
- a required fraction of that target for off-peak catch-up.

For example, a six-hour target with `required_runtime_ratio: 0.5` guarantees
three hours during the VLM day. The remaining three hours are opportunistic.

`target_finish` can bridge a short solar fluctuation when the remaining target
is no greater than `target_finish_max_remaining_h`. `target_progress` uses the
same SOC and battery-discharge guards when the target is no longer achievable:
it uses only the final configured assistance duration before the end of the
solar grace period. It improves the result without pretending the full target
can still be reached. There is no separate `target_progress` configuration
key.

## Completion and probe behaviour

`completion_mode: runtime` stops at its runtime target. A thermostatic load
continues inside an allowed energy window until measured power falls below its
active threshold, then remains complete until the daily reset.

After a commanded start, measured power must become active before
`power_confirm_timeout_s` expires:

- `probe_failure_mode: cooldown` switches the relay off and retries only after
  `probe_retry_delay_s`;
- `probe_failure_mode: lockout` switches the relay off and persists a safety
  lockout until `/data/vlm unlock <load>` is confirmed.

## Relay ownership and manual actions

VLM persists the relays it commanded ON and switches off only those owned
relays during normal policy stops or shutdown cleanup.

An output already found ON without VLM ownership is treated as external. It is
left unchanged until the daily reset, except for hard safety conditions. At
the reset it is adopted and becomes manageable.

Current limitation: if VLM originally started a load and an operator switches
it OFF externally, VLM observes the transition and drops ownership, but may
start it again after `min_off_time_s` if policy still requests it. A persistent
`manual_off_until_reset` override is planned but is not implemented.

## Battery control and balancing

Shelly control and DVCC control have independent locks. Real DVCC writes
require `battery.control_enabled: true` and global `dry_run: false`.

Normal control exposes the installation-safe charge-current ceiling below
`soc_max`, requests 0 A at `soc_max`, and reopens charging only after the SOC
falls by `charge_hysteresis_soc`.

When a balance is due, VLM waits for the normal SOC ceiling and confirmed
solar curtailment. It then keeps the protected DVCC ceiling open until
`balance_target_soc` has been held continuously for `balance_hold_s`. A
persistent deficit or the end of the solar window pauses the campaign. Its
history is independent of the daily load reset.

On controlled shutdown, enabled battery control restores
`max_charge_current_a` as a fail-open value instead of leaving a 0 A charge
limit behind.

## Cerbo communication watchdog

The communication watchdog has four states: `RECOVERING`, `OK`, `DEGRADED`
and `SAFE`. Startup begins in `RECOVERING`; complete critical telemetry reads
advance it to `OK`. Any critical failure blocks new starts. The configured
SAFE threshold stops only relays VLM owns, preserving the normal distinction
between VLM control and an external manual command. `GRID_LOST` remains a
separate hard safety and can stop an external ON relay.

SAFE mode also attempts to release a VLM-owned `Min_SOC` override and restore
`max_charge_current_a`. These operations are deliberately best-effort because
the failure being handled may make Modbus writes impossible. At the restart
threshold the process records the incident and exits; runit then recreates the
process, socket and service discovery. Normal starts remain inhibited until
the new process completes its recovery cycles.

## Persistent state

`runtime.json` is replaced atomically and contains the current VLM period,
load runtimes, probe lockouts, output transitions, completed loads, relay
ownership and battery-balancing history. Daily load state changes at
`schedule.daily_reset`; persistent motor lockouts and balancing history survive
that reset.

The file must be preserved during upgrades. `install.sh` preserves an existing
`config.yaml`, and operational deployment must likewise preserve
`runtime.json`.

## External zero-export protection

VLM does not implement an inverter-side fallback for zero-export installations.
That protection is configured independently
in the Fronius Datamanager and remains part of the surrounding operational
safety architecture.

See [Fronius zero-export fallback](fronius-zero-export-fallback.md) for the
rationale, exact settings, priority order and mandatory live test.
