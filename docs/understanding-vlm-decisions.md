# Understanding VLM decisions

## Anticipating a solar balance

Set `battery.balance_advance_days: 3` to select a favorable day up to three
calendar days before the monthly due date. During the solar window, a valid
forecast at/above `balance_good_solar_day_kwh` plus confirmed solar charging or
curtailment can start from low SOC. Logs show `solar_priority=True`; managed
loads wait with `battery_balancing`, including while holding the target,
unless optional monitored surplus sharing permits a trial.
After successful completion they return to normal operation.

At `battery.balance_solar_deadline` (default 16:00), an attempt which has not
reached its target becomes `paused_solar_deadline` and releases the loads.
An existing continuous hold may finish before the solar window ends. This
never forces grid charging; no success means no change to the monthly due date.

When a relay was switched ON externally, normal policy stops are suspended
until the daily reset. The status log shows `stop_suspended` with the original
policy reason and `external_control=protected_until_daily_reset`; no OFF command
is sent. Safety stops such as grid loss or AC overload still show `stop` and can
send OFF even for an externally controlled relay.

This page answers the operator's question: **why did a load start, remain on,
stop, or refuse to start?** It complements the
[architecture](architecture.md), [configuration reference](configuration.md)
and [troubleshooting guide](troubleshooting.md).

Examples use the reference installation: Victron ESS, Fronius AC-coupled PV,
Victron DC solar charging, a 90% normal battery ceiling and Shelly-controlled
loads.

## Reading a decision block

A typical status block has four layers:

```text
Telemetry: SOC=90.0%, AC=437 W, limit=6500 W, start_limit=6000 W,
headroom=6063 W, grid_lost=False, surplus_ready=True, deficit=False,
visible_surplus=-13 W
PV limiting: ESS=active, DC=limited [ttyS5:mode=1,power=571W],
AC_request=active, AC_limit=confirmed
Power flow: grid=..., battery=...
PoolPump: keep_off (window_end_too_close), output=OFF,
runtime=4.51/6.00 h, projected=437 W
```

Read it in order:

1. **Telemetry** establishes SOC, installation power and hard safety.
2. **PV limiting** says whether a producer is demonstrably curtailed.
3. **Power flow** says whether the site imports, exports or charges.
4. **Load decision** gives the action and stable reason code.

The reason code is the short answer. The preceding lines explain it.

## Curtailment is not low production

An inverter producing 500 W may be capped at 500 W, or may simply have only
500 W of sunlight. VLM distinguishes those cases.

### Fronius and other AC-coupled PV

VLM reads actual power, requested power limit and inverter maximum power.

- `AC_request=active`: a reduced limit was requested. This alone proves
  nothing about available sunlight.
- `AC_limit=confirmed`: positive production has settled close to the requested
  limit, within the greater of 100 W or 5% of that limit.

Example:

```text
power=754 W, limit=756 W, max=5000 W
```

Production is almost exactly at the cap: curtailment is confirmed.

By contrast, `power=310 W, limit=756 W` is unconfirmed. A cloud or weak sun
could explain the difference. A zero limit or output below 50 W is also
unknown, preventing a night-time 0/0 condition from becoming false surplus.

### Victron DC solar chargers

VLM reads `/MppOperationMode` from every solar-charger D-Bus service:

| Mode | Interpretation |
| --- | --- |
| `1` | power-limited: DC curtailment confirmed |
| `2` | normal MPPT tracking: not curtailed |
| other or missing | unknown |

Power is logged for diagnosis, but low output alone never proves curtailment.
A stopped charger in mode 0 is therefore `unknown`, not `limited`.

### Why `ESS=active` is insufficient

The aggregate `/PvPowerLimiterActive` flag is useful context, but cannot say
which generator is limited or whether an AC inverter has reached its requested
cap. VLM does not accept it alone as proof of recoverable PV.

Detailed AC/DC confirmation is available only when VLM runs locally on the GX
device and reads D-Bus. Remote Modbus exposes the aggregate flag, not the
evidence required for the same conclusion.

## From flows to usable surplus

VLM uses these signs:

- positive battery power: charging;
- positive grid power: importing;
- negative grid power: exporting.

Therefore:

```text
visible_surplus_w = battery_power_w - grid_power_w
```

| Battery | Grid | Result | Interpretation |
| ---: | ---: | ---: | --- |
| +1800 W | +100 W | 1700 W | strong charging despite small import |
| +900 W | -200 W | 1100 W | charging plus export |
| -350 W | +150 W | -500 W | battery discharge plus import: deficit |
| 0 W | +80 W | -80 W | inside a 300 W combined sample tolerance |

VLM retains the last `surplus_confirm_cycles` samples and uses their minimum.
Confirmed curtailment and deficit must also persist for their configured number
of cycles. One favourable asynchronous sample cannot finance a start.

A DVCC change clears this evidence and triggers `start_settle_s`: readings
before and after a charge-current command do not describe the same state.

## Two solar start paths

### Visible charging surplus

A load can start before PV curtailment when:

- it is inside the solar window;
- SOC is at least `soc_min + load_start_hysteresis_soc`;
- visible surplus covers `rated_w + battery_charge_reserve_w`;
- AC and ordinary start guards pass.

For a 1300 W pump and 1000 W battery reserve, at least 2300 W is required. The
reason is `solar_charge_surplus`.

### Confirmed curtailed PV

At or above `soc_on`, persistent native AC or DC curtailment can start a load
when simultaneous flows show no deficit. The reason is
`solar_surplus_confirmed`.

This case is valid:

```text
SOC=90%, visible_surplus=-13 W, surplus_ready=True, deficit=False
```

The visible balance is neutral, but a producer is confirmed at its cap.
Starting a load creates useful demand for otherwise-curtailed energy. The
tolerance absorbs meter noise; it does not authorize sustained deficit.

Insufficient persistence produces `solar_surplus_pending`.

## AC admission is a separate guard

Abundant solar never overrides the installation ceiling:

```text
measured_AC + startup_power_w
    <= max_ac_power_w - ac_start_margin_w
```

With a 6500 W limit and 500 W start margin, the guarded ceiling is 6000 W. The
pool pump reserves its 3000 W startup peak for this check, although steady
`rated_w` is 1300 W. Failure reports `ac_headroom_insufficient`.

A running load is shed only above the full 6500 W limit. Lower-priority loads
are shed first.

## Why an active load may remain on

After active power is confirmed:

- `min_on_time_s` protects against ordinary fluctuations;
- `solar_hysteresis` can keep a solar load running;
- `off_peak_required` guarantees missing required runtime;
- `target_finish` and `target_progress` continue while their guards pass.

Hard safety still wins: grid loss, invalid telemetry, AC overload, missing
power telemetry and inactive-motor faults may stop immediately. Low solar SOC
stops at `soc_off`. Minimum ON time is anti-cycling, not permission to ignore
danger.

## Required runtime versus desired target

For:

```yaml
target_runtime_h: 6
required_runtime_ratio: 0.5
```

the desired target is 6 h and the required part is 3 h. Solar is used first.
If less than 3 h has run when off-peak begins, `off_peak_required` supplies
the missing required portion before the 06:00 reset. Native ESS preserves its
own reserve; the grid takes over at that reserve.

The remaining 3 h are desirable, not mandatory. They are not blindly bought
overnight.

## `target_finish`: complete a reachable target

Given:

```yaml
target_finish_enabled: true
target_finish_max_remaining_h: 1
target_finish_min_soc: 80
target_finish_max_battery_discharge_w: 500
target_finish_grace_s: 3600
```

when required runtime is complete and no more than one hour remains, VLM can
finish during the solar window or the one-hour grace after `solar_end`.

A running load may use at most 500 W battery discharge. A new start reserves
`rated_w` against measured battery flow, unless confirmed curtailment with no
deficit proves that PV can react. Reasons are `target_finish`,
`finish_soc_low` or `finish_deficit_exceeded`.

## `target_progress`: improve an unreachable target

Captured reference case on 20 September:

```text
17:58:15  SOC=90.0%, AC=437 W, start_limit=6000 W, headroom=6063 W
17:58:15  surplus_ready=True, deficit=False, visible_surplus=-13 W
17:58:15  PoolPump: keep_off (window_end_too_close),
          runtime=4.51/6.00 h
```

At 17:58, 1.49 h remained. The 6 h target could not fit before the 19:00 grace
deadline, but abandoning the remaining opportunity was unnecessarily
pessimistic.

With one hour of assistance and grace ending at 19:00, VLM waits for the final
one-hour interval and then permits bounded best-effort operation. It does not
pretend the target will be achieved; it avoids wasting the recoverable part.
The reason is `target_progress`.

The same guards remain active:

- required runtime is already complete;
- SOC meets `target_finish_min_soc`;
- battery support stays within the configured allowance;
- AC headroom, minimum ON/OFF and start staggering pass;
- grid loss, invalid telemetry, overload and balancing remain authoritative;
- operation ends at the grace deadline even if the target is unfinished.

There is no separate `target_progress` key. The existing
`target_finish_*` settings define both the assistance budget and final
progress interval.

## Balancing and flexible loads

With `balance_surplus_enabled`, solar charging and holding can use monitored
load trials on confirmed curtailment. The log reason is `balancing_surplus`.
Battery logs expose `surplus_start`, `surplus_keep` and `surplus_charge_ref`;
loss of the protected charge reference or excessive grid import causes
`battery_balancing` to withdraw the load. This can override minimum ON time.

After successful completion, optional `post_balance_support_enabled` permits
bounded battery help before and after solar end. It stops at the desired load
target or thermostat completion, the configured SOC floor, discharge limit,
grid import limit, deadline, or hard safety guard. Relevant reasons are:

| Reason | Meaning |
| --- | --- |
| `post_balance_support` | Finish an unmet goal using the bounded post-success allowance |
| `post_balance_soc_low` | SOC reserve or start margin reached |
| `post_balance_power_limit` | Missing power telemetry or discharge budget exhausted |
| `post_balance_grid_import` | Missing grid telemetry or grid import above tolerance |
| `post_balance_deadline` | Extension expired; normal HC handling resumes at its start |

For the 16 October case, this allows a small 280 W battery discharge at 16:50
and can continue the pump at 18:00, without rearming the satisfied cumulus.
Success is persisted explicitly; an installation/reference date is not proof
of a completed hold. See the configuration reference for the opt-in limits.

Without monitored surplus sharing, the anticipated solar attempt takes exclusive
priority until its hold completes or its deadline releases the loads. The
surplus-sharing rules below apply to the existing non-exclusive campaigns.

While a balancing campaign uses the elevated target, the battery has priority.
Before the 100% hold, a load needs enough visible surplus to cover its own
`rated_w` and leave `battery_charge_reserve_w` for charging. During the
hold, confirmed curtailment with no deficit can also feed one load.

- `battery_balancing`: protected surplus is insufficient.
- `balancing_surplus`: the load may use surplus left after the reserve.

DVCC is a safe current ceiling, not actual battery charge power. The BMS may
request less while balancing cells. Every DVCC transition invalidates old flow
evidence.

## Reason-to-check map

| Reason | Meaning | First check |
| --- | --- | --- |
| `solar_surplus_pending` | curtailment is not persistent yet | following cycles and AC/DC detail |
| `solar_charge_surplus` | charging surplus funded the start | visible surplus and reserve |
| `solar_surplus_confirmed` | curtailed PV funded the start | AC/DC limiting line |
| `energy_deficit` | sustained import/discharge | grid and battery flow |
| `ac_headroom_insufficient` | startup projection is too high | AC, startup power, margin |
| `window_end_too_close` | minimum ON time no longer fits | remaining allowed window |
| `target_finish` | the target is reachable with bounded help | remaining runtime and guards |
| `target_progress` | target unreachable, useful progress possible | grace deadline and guards |
| `battery_balancing` | battery priority blocks the load | balance status and reserve |
| `balancing_surplus` | protected surplus can feed the load | surplus or hold curtailment |
| `probe_lockout` | motor ON without confirmed power | inspect, then `/data/vlm unlock` |
| `thermostat_satisfied` | heater stopped consuming | no retry before reset |

## Practical diagnostic sequence

When a load “should obviously start,” do not look at SOC alone:

1. Read its reason code.
2. Exclude safety, reachability and active-power faults.
3. Check the window and whether `min_on_time_s` still fits.
4. Check required, desired and completed runtime.
5. Check AC admission with `startup_power_w`.
6. Read detailed AC/DC curtailment evidence.
7. Read grid/battery flows, confirmation and settling.
8. If balancing is active, verify its protected reserve.
9. After a command, verify Shelly active-power confirmation.

The controller's answer is not “SOC is high, start something.” It is: **this
specific load may start now because a qualified energy source, a valid runtime
objective and every applicable safety guard agree.**
