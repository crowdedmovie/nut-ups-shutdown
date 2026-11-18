# NUT UPS shutdown

Coordinated shutdown for Debian and Proxmox VE hosts with systemd, NUT 2.8.x and
one USB HID UPS. The primary runs NUT locally. Each target needs OpenSSH,
`sudo` and a restricted shutdown account. Proxmox targets are the hosts
themselves, so their normal shutdown stops their guests.

## How shutdown works

- A sustained battery event or a sustained loss of the local UPS status starts
  an ordered shutdown. Each condition has its own delay. The primary checks
  the triggering condition twice, five seconds apart, before proceeding.
- The primary sends shutdown requests in parallel within each step. After a
  target that answered ping accepts the SSH request, three consecutive missed
  pings let the step advance early. A missed ping indicates lost reachability.
  If a request or reachability change is uncertain, the step runs to its
  timeout. The primary logs uncertain targets and proceeds. Its own shutdown
  is last.
- NUT's native emergency path uses the same sequence. It still sends SSH
  requests in step order but skips the remaining ping waits. This path can run
  earlier than either configured delay, including if NUT loses an UPS that was
  last seen on battery. In production, the native path also retains NUT's UPS
  powerdown flag for the distribution shutdown hook.

Only the primary makes shutdown decisions. A target that loses network access
stays up until it receives an order or loses power. If the primary is
unavailable, remote protection resumes after a manual promotion. A persistent
USB or local UPS status failure can trigger a precautionary shutdown even when
mains power is present. The timed paths rely on NUT to track elapsed time, so
restarting monitoring during an event can reset the timers.

## Prepare each target

Install `openssh-server` and `sudo`. Run these commands from a trusted
checkout on the target.

```sh
sudo adduser --system --group --shell /bin/sh --no-create-home \
  --home /var/lib/ups-shutdown ups-shutdown
sudo install -d -m 0755 -o root -g root /var/lib/ups-shutdown/.ssh
sudo install -m 0755 scripts/ups-ssh-command /usr/local/sbin/ups-ssh-command
sudo visudo -cf sudoers/nut-ups-shutdown-client
sudo install -m 0440 sudoers/nut-ups-shutdown-client /etc/sudoers.d/
```

On each intended primary, install the packages below and create a dedicated
SSH key under the root-owned configuration directory. An unattended service
needs access to this key at shutdown time. Protect the private key and give
each potential primary its own key.

```sh
sudo apt install nut-server nut-client sudo openssh-client iputils-ping iproute2 python3
sudo install -d -m 0700 /etc/nut-ups-shutdown
sudo ssh-keygen -t ed25519 -N '' -f /etc/nut-ups-shutdown/ssh_key
```

In `/var/lib/ups-shutdown/.ssh/authorized_keys` on each target, install the
public key of each authorized primary with these options. Keep this file and
its parent directories owned by root. Give the file mode `0644` so OpenSSH
can read it as the target account.

```text
restrict,command="/usr/local/sbin/ups-ssh-command" ssh-ed25519 <primary-public-key>
```

Populate `/etc/nut-ups-shutdown/known_hosts` on each primary with the target
SSH host keys, verified independently, for example from the target consoles.
Use the same host names or IP addresses as in the configuration. SSH runs on
port 22. Host keys are checked on every connection, including when IP
addresses are used.

## Configure and activate the primary

1. Copy [config.env.example](config/config.env.example) to
   `/etc/nut-ups-shutdown/config.env` as root with mode `0600`, then edit it.
   Use IP addresses to keep shutdown independent of local DNS. DNS names are
   also supported. IPv6 literals use plain notation without brackets.
   `ON_BATTERY_DELAY_SECONDS` and `UPS_UNREACHABLE_DELAY_SECONDS` set the
   timers for battery operation and loss of the local UPS reading.
2. List each remote target once in `SHUTDOWN_TARGETS`. In `SHUTDOWN_ORDER`,
   separate steps with spaces and parallel hosts within a step with commas.
   For example, `"192.0.2.21,192.0.2.22 192.0.2.30"` waits for the first
   two hosts together, then for the third host by itself. Each step gets up to
   `SHUTDOWN_STEP_TIMEOUT_SECONDS`. The primary always shuts down locally
   at the end. For dependencies such as shared storage, add
   `consumer>provider` pairs to `SHUTDOWN_DEPENDENCIES`. The validator rejects
   any pair whose consumer is in the same step as, or later than, its provider.
3. Run `sudo bash scripts/promote-primary --check` from the trusted checkout.
   This prints the order and checks SSH and ping without requesting a poweroff.
   Unavailable ping is allowed and makes the relevant step wait to its timeout.
4. With the UPS USB cable attached, run `sudo bash scripts/promote-primary`.
   The script validates the candidate and checks the clients before replacing
   the dedicated single-UPS NUT configuration. It makes a root-only backup of
   the previous files under `/var/backups`, installs the project files, and
   starts the monitor after confirming a live reading from the UPS. On
   activation failure it attempts to restore the previous files and service
   state. The NUT data server listens on loopback.

The script is for hosts where this project owns the local NUT configuration.
It configures the `usbhid-ups` driver. Apply changes to `config.env` by running
the script again so that the active configuration and NUT timers match. An
empty target inventory and order enable local-only shutdown.

## Validate before enabling poweroff

Leave `TEST_MODE=true` for the first deployment. All three triggers use the
non-destructive SSH check in this mode, and the generated NUT configuration
omits the hardware powerdown flag. Review logs with
`journalctl -u nut-monitor -u 'ups-emergency-shutdown@*.service'`.

- Restore mains before the battery delay expires and check that the timer is
  cancelled. With a longer interruption, check that the planned order is
  logged after the delay and status verification.
- Test a loss and recovery of the UPS USB connection and a persistent loss.
  Verify the former cancels the pending shutdown and the latter logs the
  expected sequence.
- In a test deployment, exercise the native path with `sudo upsmon -c fsd`.
  Restart `nut-server` and `nut-monitor` afterwards to clear NUT's latched
  forced-shutdown state and resume monitoring.
- Measure battery runtime under load. Allow for the initial delay, the sum of
  the step timeouts, and the time each host and its guests take to shut down.
  Set `TEST_MODE=false`, apply the configuration and schedule a real test
  before relying on the setup.

## Promote a replacement manually

1. Prepare the replacement's packages, key, trusted host keys and
   `config.env` ahead of time. Set `PRIMARY_HOST` to the replacement's
   address and remove its address from both the inventory and the order.
   Update the dependencies and decide whether the outgoing primary should
   remain as an SSH target. Run `sudo bash scripts/promote-primary --check`
   from the checkout to inspect the new order.
2. If the old primary is reachable, do the handover while on mains. Verify
   that no shutdown sequence is active. Disable and stop its
   `nut-monitor.service` and `nut-server.service` before unplugging USB so
   it cannot treat the move as a persistent loss and send shutdown orders.
   Keep it disabled if it later returns from maintenance.
3. Move the USB cable. If the setup keeps storage on the primary, move the
   storage and verify that its consumers can use it there. Start the new
   primary with `sudo bash scripts/promote-primary` and `TEST_MODE=true`.
   Check the UPS reading and the printed shutdown order, then enable
   production mode once validated.
4. Remove the old primary's public key from the targets when that host
   should no longer be able to issue shutdown orders.
