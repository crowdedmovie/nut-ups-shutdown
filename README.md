# NUT UPS shutdown

Debian + NUT 2.8.x example for an APC Back-UPS BX1600MI attached over USB.

## Logic

- `upsmon` sends `ONBATT` or `COMMBAD` to `upssched`, which starts a **60-second** timer.
- `ONLINE` and `COMMOK` cancel their respective timers.
- On timer expiry, the event handler checks the UPS again and asks a narrowly authorized **root-owned systemd service** to shut down.
- The service checks the UPS twice, five seconds apart. If still on battery or unreachable, it shuts down the OS.
- NUT's native low-battery/FSD shutdown remains enabled independently.

**Limitations:** The privileged service does **not** independently verify 60 elapsed seconds; it trusts `upssched`. Timers can be lost on monitoring restarts. A direct service invocation during an outage can initiate shutdown after five seconds. A future revision should enforce elapsed time in the privileged component.

## Files

- `config/nut/*.example`: sanitized NUT configuration snippets/templates.
- `scripts/ups-event-handler`: unprivileged timer callback.
- `scripts/ups-final-shutdown`: root-owned status verification and shutdown.
- `systemd/ups-emergency-shutdown.service`: privileged oneshot unit.
- `sudoers/nut-ups-shutdown`: restricted permission for user `nut`.

## Installation outline

1. Install NUT, set `MODE=standalone`, and configure `usbhid-ups` for your USB UPS.
2. Create a local NUT monitoring user with a **unique password** in `/etc/nut/upsd.users`. Use the same credentials in `/etc/nut/upsmon.conf`. **Never commit either real credential.**
3. Merge example NUT directives into your existing distro configuration. Do not overwrite required distro defaults.
4. Copy scripts to `/usr/local/sbin/` as `root:root`, mode `0755`. Install the unit under `/etc/systemd/system/` and run `systemctl daemon-reload`. Install sudoers via `visudo -f /etc/sudoers.d/nut-ups-shutdown`.
5. Check `/run/nut` pipe/lock permissions for `upssched`. Restart `nut-monitor` while UPS status is `OL`.
6. Test with `TEST_MODE=true` in the privileged script and a harmless temporary NUT `SHUTDOWNCMD`; validate recovery, USB loss, and mains loss. **This does not disable NUT's own emergency shutdown or the UPS hardware.**
7. For production, set `TEST_MODE=false`, restore `SHUTDOWNCMD "/usr/sbin/shutdown -h now"`, and schedule a real shutdown/restart test.

These files are **sanitized reconstructions of the discussed configuration**, not direct exports from the server. Review all settings and paths before deployment.
