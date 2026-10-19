# Getting started on Cerbo GX

This guide is for a first Versatile Load Manager installation on a Cerbo GX. It
uses plain language and begins in a mode that cannot command a Shelly or change
the battery charge limit.

## Release package

Use the `victron-load-manager-VERSION-venus.tar.gz` file published with a
release, not GitHub's automatically generated source archive. The Venus package
contains the checked offline Python dependencies in `vendor/`; the source
archive does not.

The computer is used only to download this file and transfer it to the Cerbo.
It does not need Python, Git, `pip` or a development environment.

### Transfer from Windows

In a recent PowerShell:

```powershell
scp .\victron-load-manager-VERSION-venus.tar.gz root@CERBO_ADDRESS:/data/
ssh root@CERBO_ADDRESS
```

WinSCP may be used instead of `scp` for the file transfer.

### Transfer from Linux or macOS

```sh
scp victron-load-manager-VERSION-venus.tar.gz root@CERBO_ADDRESS:/data/
scp victron-load-manager-VERSION-venus.tar.gz.sha256 root@CERBO_ADDRESS:/data/
ssh root@CERBO_ADDRESS
```

After the SSH connection is open, every following command runs on the Cerbo
and is identical for all three computer platforms.

### Verify and extract on the Cerbo

```sh
cd /data
sha256sum -c victron-load-manager-VERSION-venus.tar.gz.sha256
tar -xzf victron-load-manager-VERSION-venus.tar.gz
cd victron-load-manager-VERSION-venus
```

Do not continue if the checksum is not reported as `OK`. The final `cd`
places the shell in the extracted package directory containing `install.sh`.

## Information to collect first

| Real installation fact | Where it is used |
| --- | --- |
| Maximum safe installation AC power | `max_ac_power_w` |
| Normal minimum and maximum battery SOC | `battery.soc_min` and `soc_max` |
| Safe shared battery-cable/fuse current | `battery.max_charge_current_a` |
| Solar-management hours | `schedule.solar_start` and `solar_end` |
| Off-peak start and daily reset | `off_peak_start` and `daily_reset` |
| Shelly address and output number | each load's `host` and `switch_id` |
| Normal and starting power of each load | `rated_w` and `startup_power_w` |
| Desired and mandatory daily duration | runtime target and required ratio |

The DVCC current is a safety value for the complete DC path, not the sum of
the battery modules' advertised capability. Use the weakest shared cable,
fuse, busbar or other protected component.

## Safe test installation beside production

The installer accepts separate paths, allowing its file layout to be tested
without replacing the production service:

```sh
VLM_TARGET_DIR=/data/vlm-install-test \\
VLM_SERVICE_LINK=/service/vlm-install-test \\
VLM_COMMAND_LINK=/data/vlm-test \\
VLM_UPDATE_RC_LOCAL=0 \\
./install.sh
```

This creates a separate service and command, leaves the service down and does
not make the test links persistent in `/data/rc.local`. The test service must
remain stopped while the production VLM is running.

## Normal installation

With a compatible `vendor/` directory beside the installer:

```sh
chmod +x install.sh
./install.sh
```

The installer copies VLM to `/data/victron-load-manager`, preserves an existing
`config.yaml` and leaves the service stopped.

## First configuration: keep all locks closed

Edit `/data/victron-load-manager/config.yaml`. During commissioning, retain:

```yaml
dry_run: true
control_enabled: false
battery:
  control_enabled: false
```

- `dry_run` is the global block on every write.
- `control_enabled` authorizes Shelly load commands only.
- `battery.control_enabled` separately authorizes DVCC changes.

For a shorter starting file, copy the validated minimal template before editing:

```sh
cp /data/victron-load-manager/config/config.minimal.yaml \
  /data/victron-load-manager/config.yaml
chmod 600 /data/victron-load-manager/config.yaml
```

The minimal template contains one disabled-by-permission example load and marks
the installation-specific values with `REVIEW`. In particular, replace the AC
limit, SOC thresholds, safe DVCC current and Shelly details. Defining a load does
not authorize it: `managed_loads` remains empty and all three write permissions
remain closed.

The full annotated example remains available as
`config/config.example.yaml`. Configure only known values; the meaning and safe
ranges are described in the [configuration reference](configuration.md).

## Check without commanding anything

```sh
/data/vlm check
```

This validates the YAML, reads the Cerbo, discovers its Victron services and
checks every enabled Shelly. It displays the current command permissions and
does not send a relay or DVCC command.

Do not continue until the result is `OK (read-only)` and each intended Shelly
is reported as a switch rather than a meter or unreachable device.

## Observe continuously

```sh
/data/vlm enable
tail -f /data/victron-load-manager/logs/current
```

Leave VLM in observation mode long enough to see its decisions under real sun,
battery and load conditions. A planned command is logged but not executed.

A temporary safe stop, which still allows automatic restart after the next
Cerbo reboot, is:

```sh
svc -d /service/victron-load-manager
```

To stop VLM and keep it disabled across reboots:

```sh
/data/vlm disable
```

`/data/vlm enable` removes the service's persistent `down` marker before starting
it. The installer also records the service link in `/data/rc.local`, so Venus
OS recreates the supervised service and starts it automatically after a reboot.

## Progressive activation

Use this order:

1. read-only `/data/vlm check`;
2. continuous observation with every control lock closed;
3. guarded commissioning of one suitable load;
4. add only that exact load to `managed_loads`;
5. set `dry_run: false` and global `control_enabled: true`;
6. observe several real cycles before adding another load;
7. enable `battery.control_enabled` only after load control is validated and
   the safe DVCC current has been independently checked.

Never run a manual H24 instance beside the supervised service. Preserve
`runtime.json` during updates: it contains runtimes, relay ownership, safety
lockouts and balancing history.

## Maintainer release procedure

The following section is for project maintainers, not first-time installers.

### Building the offline package

End users do not run these commands. On the release-building computer:

```sh
python3 tools/build_vendor.py
tools/build-venus-release.sh --vendor build/vendor
```

`requirements-venus.lock` freezes every dependency included in a release.
The builder downloads the PyYAML source and copies only its portable Python
package; it does not include the optional native extension. All other locked
dependencies must be available as pure-Python wheels. The validator rejects
compiled libraries, generated caches and missing packages before the release
archive is created.

The GitHub workflow `Build Venus package` runs the same chain. A manual run
creates a downloadable workflow artifact without publishing a release. Pushing
a tag matching the code version, for example `v0.1.0` for version `0.1.0`,
also creates the GitHub Release and attaches the archive and checksum. A
mismatched tag is rejected before publication.

Recommended release sequence:

1. update `__version__` and the release notes;
2. run the complete test suite on `main`;
3. trigger the workflow manually and inspect its archive;
4. create and push the matching `vX.Y.Z` tag;
5. verify the published checksum before installation testing.
