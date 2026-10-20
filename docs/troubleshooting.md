# Troubleshooting

For a field-oriented explanation of the power calculations and policy reasons,
see [Understanding VLM decisions](understanding-vlm-decisions.md).

## First checks

```sh
svstat /service/versatile-load-manager
tail -f /data/log/versatile-load-manager/current
/data/vlm faults
```

The startup line identifies the package version and absolute source path. Use
it to confirm that the installed service, rather than a copied test tree, is
running.

## Safe service commands

```sh
# Stop gracefully. Owned ON relays are switched off and enabled DVCC control
# restores max_charge_current_a as a fail-open value.
svc -d /service/versatile-load-manager

# Start the installed service.
svc -u /service/versatile-load-manager

# Restart after a configuration or source update.
svc -t /service/versatile-load-manager
```

Never launch a second manual H24 process alongside the supervised service. The
process lock refuses it, but duplicate commissioning commands should still be
avoided.

## Diagnostic commands

```sh
vlm --help
/data/vlm faults
/data/vlm unlock PoolPump
```

`/data/vlm faults` is read-only. `/data/vlm unlock` asks for confirmation, pauses a running
service, clears only the selected persistent motor lockout, and restores the
service state. Inspect the motor, hydraulic circuit, contactor and Shelly power
telemetry before clearing a lockout.

The full module CLI also provides commissioning commands. Display its exact
arguments before use:

```sh
PYTHONPATH=/data/versatile-load-manager/vendor:/data/versatile-load-manager/src \
  python3 -m versatile_load_manager --help
```

## Common decision reasons

| Reason | Interpretation |
| --- | --- |
| `unreachable` | Shelly output state could not be read; no state is guessed |
| `telemetry_invalid` | SOC or installation AC telemetry is unusable |
| `grid_lost` | Grid-loss safety dominates normal scheduling |
| `ac_overload` | Active load is shed to return below the AC ceiling |
| `soc_low` | Solar operation reached the configured SOC stop level |
| `solar_surplus_pending` | Native curtailment has not persisted long enough |
| `solar_surplus_confirmed` | Confirmed curtailment permits a start |
| `solar_charge_surplus` | Measured charging surplus can feed the load while preserving battery reserve |
| `energy_deficit` | Persistent import or battery discharge stopped solar operation |
| `start_staggered` | Another start or a DVCC transition is still settling |
| `ac_headroom_insufficient` | Startup projection exceeds the guarded start ceiling |
| `window_end_too_close` | Remaining window is shorter than `min_on_time_s` |
| `min_on_time` / `min_off_time` | Anti-cycling delay is active |
| `off_peak_required` | Required runtime is being guaranteed before the reset |
| `runtime_complete` / `daily_completed` | Runtime or daily completion objective is satisfied |
| `thermostat_pending` | Thermostatic cycle must continue until measured power falls |
| `thermostat_satisfied` | Relay drew no power because the heat cycle completed |
| `probe_cooldown` | Failed start probe is waiting before a retry |
| `probe_lockout` | Persistent motor safety lockout requires inspection and explicit unlock |
| `battery_balancing` | Battery has priority and protected surplus is insufficient |
| `balancing_surplus` | Load may use surplus left after the battery reserve |
| `target_finish` | Safely completing a reachable final portion of the desired target |
| `target_progress` | Safely improving an unreachable target during the final assistance interval |
| `finish_soc_low` | Finishing assistance is blocked by its SOC guard |
| `finish_deficit_exceeded` | Required battery support exceeds the configured allowance |
| `outside_allowed_window` | Neither solar, off-peak nor finishing grace permits operation |

## A load does not start

Check, in this order:

1. `enabled`, exact membership in `managed_loads`, `control_enabled` and
   `dry_run`.
2. The decision reason in the latest status block.
3. Shelly reachability, output state and active power.
4. Remaining `min_off_time_s` and global `start_settle_s`.
5. AC start headroom using `startup_power_w`.
6. Solar surplus confirmation, SOC and battery/grid flows.
7. Runtime already completed or too little time left in the window.
8. Persistent lockout reported by `/data/vlm faults`.

## A manually changed relay moves again

An externally observed ON relay without VLM ownership is left unchanged until
the daily reset, apart from hard safety stops.

An OFF intervention is not yet a persistent manual override. If VLM originally
owned the running relay, it records the OFF transition but may start the load
again after `min_off_time_s`. To hold all relays without disabling battery
policy, temporarily set global `control_enabled: false` and restart VLM. Restore
the value and restart again when normal load control should resume.

## Runtime appears wrong

Runtime grows only while the Shelly is observed ON and measured power is
active. Compare the status timestamp, `state_for`, measured Shelly power and
the stored `runtime_h` values.

Do not replace `runtime.json` during deployment. If investigation requires a
copy, stop the service or copy the atomically written file without editing it.
The VLM day begins at `daily_reset`, not at midnight.

## Modbus service discovery fails

Confirm that Modbus TCP is enabled on the GX device, the configured host and
port are reachable, and the scan range contains the system and VE.Bus service
IDs. Keep both IDs set to `auto` for normal portable operation.

The service cache is an optimisation. If logs prove it is stale and automatic
validation cannot recover, stop VLM, move the cache file aside, and restart so
the services are scanned again. Preserve `runtime.json`; it is unrelated.

## Repeated `WATCHDOG` messages

`DEGRADED` means a critical Cerbo operation failed and new starts are already
blocked. `SAFE` means the consecutive-error threshold was reached; VLM stops
its owned loads and attempts to restore `Min_SOC` and DVCC. A
`restart requested` line followed by a new startup is the configured runit
recovery after repeated failures, not a Cerbo reboot.

Check the error count and original exception in the same log line. Persistent
incident evidence, restart count and the latest recovered history are stored
under `watchdog` in `runtime.json`. Do not erase that file merely to clear the
message; correct the Modbus path, GX service availability or network problem.

## Balancing does not start

A due date alone is insufficient. VLM also requires the solar window, normal
SOC ceiling, confirmed PV curtailment and no persistent deficit. Inspect the
Battery status line for `not_due`, `waiting_surplus`, `charging`, `holding` or a
paused state, together with the reported next date and DVCC transition.

Real writes additionally require `battery.control_enabled: true` and
`dry_run: false`.

## Collecting a soak-test archive

The installed logger uses rotated files under
`/data/log/versatile-load-manager`. Ensure retention covers the intended test
period before starting a long soak. For a two-week investigation, 50 MB is a
reasonable conservative allocation for the current status volume:

```sh
sed -i 's/s1000000 n10/s5000000 n10/' \
  /data/versatile-load-manager/service/log/run
svc -t /service/versatile-load-manager/log
```

After the observation period, collect logs together with the state and the
configuration that produced them:

```sh
tar -czf /data/vlm-soak.tar.gz \
  -C /data \
  versatile-load-manager/runtime.json \
  versatile-load-manager/config.yaml \
  log/versatile-load-manager
```

The archive may contain hostnames, addresses and operational history. Review
its destination before sharing it.

## Fronius fallback verification

The reference installation uses inverter-side dynamic power reduction as a
defence against loss of Cerbo Modbus commands. The complete Datamanager
configuration, priority order and live-test procedure are documented in
[Fronius zero-export fallback](fronius-zero-export-fallback.md).
This cannot be validated from a VLM log alone. Test both transitions under
safe site conditions:

1. interrupt Cerbo/Fronius communication;
2. verify that the Modbus timeout expires and production falls to the intended
   safe level;
3. restore communication;
4. verify that Modbus automatically regains priority and normal regulation.

This test has been completed successfully on the reference installation; it
must be repeated after material inverter, meter or network configuration
changes.
