# Tempo and solar-forecast design

## Status

The Tempo and lightweight Open-Meteo solar providers use dedicated caches.
Optional solar balancing anticipation now uses a valid current-day forecast
alongside live solar evidence; the HC catch-up planner also uses forecasts.
Dynamic reserve calculation and general forecast-based load scheduling remain
future extensions. See the configuration reference for implemented options.

## Objective

Tempo awareness must improve when energy is stored or consumed without making
load safety depend on an Internet service. Decisions must combine:

- today's and tomorrow's Tempo colour;
- off-peak and peak periods;
- hourly solar-production forecast;
- current SOC, battery power and measured PV conditions;
- remaining required and desired load runtimes;
- a configurable household reserve.

Tempo colour alone is insufficient. A red day followed by strong sun does not
require the same reserve as a red day followed by a poor forecast. Conversely,
a blue day does not justify exhausting the battery when the following morning
is expensive and dark.

## Separation of responsibilities

The extension should have four independent layers:

1. **Tempo provider** retrieves raw current and next-day colour information.
2. **Solar forecast provider** retrieves an hourly power or energy forecast.
3. **Persistent cache** stores normalized, timestamped provider results.
4. **Tariff policy** consumes a frozen snapshot and produces explainable
   reserve and load decisions.

Providers never command DVCC or a load. Network access must not occur inside
the pure load-policy evaluator.

## Normalized inputs

### Tempo state

The normalized Tempo state currently exposes:

- local date;
- current colour: `blue`, `white`, `red` or `unknown`;
- next-day colour with the same states;
- provider observation time;
- cache origin and age;
- freshness status and diagnostic detail.

A future manual correction must be visible in logs, expire automatically and
never silently overwrite freshly retrieved source data.

### Solar forecast

The implemented first stage requests hourly global tilted irradiance for one
configured orientation and converts it to whole-installation energy with an
effective peak power and a fixed performance ratio. This deliberately avoids
per-inverter modelling, VRM history, a database and automatic calibration.
The reference installation uses an effective 8 kWp for its co-oriented AC- and
DC-coupled arrays.

Every forecast snapshot includes:

- issue time and covered interval;
- hourly GTI values in the configured local timezone;
- source name and whether a cached result is being used;
- age and validity status.

## Cache and availability

Provider responses are normalized and written atomically to dedicated cache
files. Runtime and balancing history remain in `runtime.json`; remote data is
not mixed into that safety-critical state.

The solar provider currently exposes these validity states:

- `valid`: recent data covering today and tomorrow;
- `stale`: usable only for display or a deliberately conservative fallback;
- `unavailable`: no trustworthy data;
- `incomplete`: valid data which does not cover both required dates.

Provider failures must use bounded timeouts and backoff. They must not delay
the H24 control loop.

## Policy outputs

The tariff policy should produce inputs for the existing battery and load
policies rather than bypassing them:

- `target_reserve_soc` for the upcoming expensive interval;
- permission or preference to complete flexible runtime now;
- latest safe completion deadline for each required load;
- a reason code containing Tempo colour, forecast status and reserve basis.

Existing hard guards remain authoritative: grid loss, invalid local telemetry,
AC ceiling, active-power faults, motor lockout, minimum ON time and BMS/ESS
limits.

## Reserve calculation principles

The first implementation should remain bounded and explainable instead of
attempting global optimization. A suitable calculation can use:

1. estimated household energy until the next cheap interval;
2. required load energy that cannot safely wait;
3. conservative forecast PV energy before and during that interval;
4. usable battery capacity and configured minimum SOC;
5. an uncertainty margin increasing with forecast age and poor confidence.

The resulting reserve must be clamped between configured minimum and maximum
values. Forecast energy should be discounted rather than treated as certain.
The discount must become more conservative for red days and as forecast
quality degrades.

VLM must not raise ESS minimum SOC merely to charge from the grid unless a
future configuration explicitly authorizes that separate behaviour. The first
version should influence charge retention and flexible-load timing only.

## Load scheduling principles

- Required runtime remains guaranteed before its deadline.
- Desired runtime may be advanced when solar is forecast to be insufficient
  later, provided the tariff and reserve permit it.
- Desired runtime may wait when sufficient low-cost or solar energy is
  forecast with adequate confidence.
- A red peak period should not start an optional load from the grid merely to
  satisfy a desired target.
- `target_finish` and `target_progress` remain bounded last-resort mechanisms;
  forecast logic should reduce how often they are needed, not remove their
  local safety guards.
- Battery balancing remains solar-only and may be postponed when the forecast
  cannot provide a credible surplus window.

## Safe degraded modes

| Tempo data | Solar forecast | Behaviour |
| --- | --- | --- |
| Fresh | Fresh | Full tariff- and forecast-aware policy |
| Fresh | Missing/stale | Tempo-aware conservative reserve; no assumed future solar |
| Unknown | Fresh | Fixed-hours tariff behaviour; forecast may inform only bounded solar opportunities |
| Unknown | Missing/stale | Existing fixed-hours policy without optimization |

Unknown remote data must never stop required off-peak catch-up or disable hard
safety. It should reduce optimization, not availability.

## Logging and observability

Each policy change should log:

- current and next Tempo colour plus source age;
- forecast energy, horizon, age and confidence classification;
- calculated reserve SOC and its clamping limits;
- load deadline or deferral decision;
- explicit fallback reason when remote data is unusable.

The daily activity report should retain the inputs that explain a decision,
not only its final reason code.

## Implementation stages

1. **Implemented:** add normalized Tempo and solar models, background providers
   and atomic caches without changing the active policy.
2. **Current stage:** run both providers in observation mode and compare the
   approximate solar output with measured PV.
3. Add calculated reserve and load recommendations to logs only.
4. Validate several blue, white and red days, including provider outages.
5. Enable one bounded policy effect at a time behind an independent
   configuration lock.

## Acceptance criteria

- Loss of Internet or either provider leaves existing local control intact.
- Dates, day boundaries and daylight-saving transitions use
  `schedule.timezone`.
- A stale tomorrow colour is never presented as fresh current data.
- A forecast cannot directly command a relay or DVCC.
- Every reserve or scheduling change has an inspectable reason and input age.
- Simulation tests cover optimistic forecast error, pessimistic forecast
  error, missing data and colour transitions.

