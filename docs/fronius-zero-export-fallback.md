# Fronius zero-export fallback

## Scope

This procedure describes a Fronius-side fallback for Victron ESS installations
operating under a French CACSI zero-export condition. It protects the
installation if communication between the Victron Cerbo and an AC-coupled
Fronius inverter is lost.

The mechanism is configured entirely in the Fronius Datamanager. VLM neither
implements nor supervises it.

## Why the fallback is required

The CACSI operating condition prohibits deliberate export to the public grid.
During normal operation, the Victron system measures site flow and sends an
active-power limit to the AC-coupled Fronius through Modbus TCP. Modbus has the
highest command priority, so this limit controls the Fronius while
communication is healthy.

Loss of the Ethernet path does not immediately stop the inverter. The Fronius
holds the last Modbus power command for the configured 120-second revert time.
Without a second control source, expiry of that command can release the
limitation and allow the Primo to produce the maximum solar power then
available, up to its 5 kW AC ceiling. If local consumption cannot absorb that
power, the surplus is exported and the CACSI zero-export condition is no
longer respected.

Dynamic power reduction closes this failure path. It has lower priority than
Modbus, so it does not interfere with normal Victron regulation. Once the
Modbus command expires, it becomes the highest available command and applies a
0% grid-feed-in target from the Fronius measurement path. When communication
returns, higher-priority Modbus automatically takes control again.

The protection chain is therefore:

1. healthy network: Victron/Cerbo Modbus regulation;
2. network lost, first 120 seconds: last Modbus command held;
3. Modbus command expired: Fronius dynamic zero-feed-in control;
4. network restored: automatic return to Modbus regulation.

This fallback covers loss of the Cerbo/Fronius command path only while the
Fronius Datamanager and its grid-measurement path remain operational. In the
validated configuration, **Reduce inverter power to 0% if meter connection is
cut** is disabled; loss of the meter itself is therefore a separate failure
case and is not validated by the Cerbo-network test.

## Prerequisites

- Fronius Modbus control must already operate normally from the Cerbo.
- The SunSpec active-power command revert timeout
  (`WMaxLimPct_RvrtTms`) must be set to 120 seconds and verified on the live
  inverter.
- The grid-measurement path used by Fronius dynamic reduction must be working.
- Record the existing DNO editor settings before changing them.

## Datamanager dynamic reduction

Open **Settings > DNO Editor**, unfold **Dynamic power reduction**, and enter
values independently validated for the installation:

| Setting | Value |
| --- | --- |
| Power reduction | `Limit Entire System` |
| Total DC power of the installation | site-specific Fronius-connected DC nameplate power |
| Export Limit Protection (Hard Limit Trip) | Disabled |
| Export Limiting Control (Soft Limit) | Enabled |
| Maximum Grid Feed-In Power | `0 %` |
| Reduce inverter power to 0% if meter connection is cut | Disabled |

Use only the DC nameplate power connected to the Fronius covered by this
Datamanager setting. Do not substitute its AC output ceiling or the total site
PV power when other arrays are connected to separate Victron MPPTs. Save the
block with its confirmation check mark.

`0 %` is a zero **grid feed-in** target, not necessarily an absolute 0 W
inverter-output target. When valid local consumption and meter data remain
available, dynamic reduction may still supply that consumption.

## Command priorities

In **Command priorities**, arrange the sources as follows and save the block:

1. **Command via Modbus** — maximum priority;
2. **Dynamic power reduction** — medium priority;
3. **Ripple-control receiver** — minimum priority.

No IO4 bridge or additional receiver cable is used for this fallback. During
normal operation, Modbus remains authoritative. If its command expires after
120 seconds, dynamic power reduction becomes the highest available source and
enforces the zero-feed-in target.

## Mandatory live test

Perform the test while directly observing both Fronius output and grid flow:

1. With the network intact, confirm that Fronius follows the Cerbo Modbus
   power request.
2. Interrupt only the Cerbo/Fronius communication path; do not disconnect the
   inverter from AC or disturb the measurement path needed by dynamic
   reduction.
3. During the first 120 seconds, confirm the expected Modbus hold/revert
   behaviour.
4. After the timeout, confirm that dynamic reduction takes control and that
   grid export remains at the permitted value.
5. Restore the network and confirm that Modbus automatically regains priority
   without a Datamanager or inverter restart.

Record the observed transition time, inverter output and grid flow for the
installation. Repeat the complete test after any material inverter, meter,
firmware, network or DNO editor change; example measurements from another site
are not acceptance evidence.
