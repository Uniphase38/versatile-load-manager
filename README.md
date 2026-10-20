# Versatile Load Manager (VLM)

Versatile Load Manager is an independent H24 controller for flexible electrical
loads on Victron Venus OS installations. It uses local Victron energy data to
schedule Shelly-controlled loads while respecting battery reserve, AC limits,
runtime targets, allowed windows and hard safety conditions.

Typical loads include water heaters, pool pumps and other deferrable equipment.
VLM runs locally on a Cerbo GX or remotely on a Raspberry Pi; Node-RED is not
required.

> VLM is an independent community project. It is not an official Victron Energy
> or Shelly product and is not affiliated with or endorsed by either company.

## Safety first

A fresh installation cannot command a relay or change the DVCC charge-current
limit. Three explicit permissions protect commissioning:

- `dry_run: false` permits writes globally;
- `control_enabled: true` permits Shelly commands;
- `battery.control_enabled: true` separately permits DVCC writes.

Keep all three locks closed during the first observation period. VLM also blocks
starts on invalid telemetry or grid loss, enforces AC headroom, supervises relay
power and preserves relay ownership across restarts.

## Start here

1. Read the [Cerbo GX getting-started guide](docs/getting-started-cerbo.md).
2. Install the offline Venus package from a project release.
3. Configure the real installation while keeping every control lock closed.
4. Run `/data/vlm check`.
5. Observe decisions continuously before enabling one load at a time.

The installer creates `/data/versatile-load-manager`, installs a native runit
service and leaves it stopped. It never overwrites an existing `config.yaml`.

## Main capabilities

- automatic discovery of Victron Modbus services;
- priority, runtime, seasonal target and scheduling rules for multiple loads;
- solar-surplus allocation with AC headroom and anti-cycling protection;
- required-runtime catch-up during off-peak hours;
- solar-first Pylontech balancing through guarded DVCC control;
- persistent runtime, lockout, relay-ownership and balancing state;
- Tempo and PV-forecast inputs without making network access part of the
  safety-critical control loop;
- watchdog recovery and supervised automatic restart on Venus OS.

## Documentation

- [Getting started on Cerbo GX](docs/getting-started-cerbo.md)
- [Understanding VLM decisions](docs/understanding-vlm-decisions.md)
- [Architecture and safety model](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Detailed behavior reference](docs/behavior-reference.md)
- [Troubleshooting and log collection](docs/troubleshooting.md)
- [Tempo and solar-forecast design](docs/tempo-design.md)
- [Reference installation: Fronius zero-export fallback](docs/fronius-zero-export-fallback.md)

## Technical names

The project, Python package, installation directory, service and release
archives consistently use `versatile-load-manager` (or
`versatile_load_manager` where Python requires underscores). The short
administrative command remains `/data/vlm`.

## License

Versatile Load Manager is licensed under the
[Mozilla Public License 2.0](LICENSE) (`MPL-2.0`).

Copyright (c) 2026 Bertrand Feuerstein.

Modifications to MPL-covered VLM files that are distributed must remain
available under the MPL 2.0. Separate files may be combined with VLM under other
licenses as permitted by the MPL. See [NOTICE](NOTICE) for attribution.

## Project status

VLM is under active development and should remain in read-only observation mode
until its decisions have been checked against the real installation. Review the
safety model and configuration reference before enabling commands.
