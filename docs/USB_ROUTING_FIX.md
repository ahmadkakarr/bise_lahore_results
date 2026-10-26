# USB identity and route isolation

## 2026-10-23: false offline transitions after portal socket errors

The Model Town run starting at `20261023T073040976663Z` recorded mobile
`RemoteProtocolError`, `WriteTimeout`, `ReadTimeout` and safe form GET
`ConnectTimeout` failures. `RouteHealth.invalidate()` immediately marked every
such failure as `offline`, before checking the interface. Kernel/NetworkManager
logs show the last USB reconnection at 12:30:16 Asia/Karachi and no USB removal
or mobile DHCP change during the later failure interval starting around 12:38.
The stable phone identity rule and isolated NetworkManager profile are installed.
Two independent read-only checks per route subsequently fetched the portal with
HTTP 200 and passed the bound public-IP echo. These observations establish the
misleading health classification; they do not establish whether the individual
socket failures originated in the mobile upstream, server or an idle connection.

Health now distinguishes local link/source readiness, portal transport errors
and echo-service reachability. Concurrent request errors request one recheck
and show `degraded`; they do not fabricate USB disconnects. On an unchanged,
previously verified route, the first two echo failures allow existing work to
continue. Three consecutive echo failures without a portal success in the last
30 seconds pause that route as `unreachable`, with automatic rechecks. A recent
successful form GET or confirmed portal outcome is positive reachability
evidence. Initial/unverified routes, changed source addresses, removed devices
and shared-egress conflicts cannot use this grace. Actual missing interfaces
remain `offline`; inventory failures show `checking`; configuration conflicts
show `paused`. Other routes continue independently.

A slow periodic echo no longer holds all workers behind its lock on an unchanged
verified source. Failed dispatched POSTs retain their uncertain receipts; their
worker replaces its socket pool before the next fresh form GET. No failed POST
is automatically replayed by the transport. Existing explicit, bounded,
same-route uncertain retry policy and server cooldowns remain in force. A
successful IP echo alone does not reset portal backoff.

Read-only evidence: `artifacts/mobile-health/ReadOnly-Probes.json`. Focused
regressions cover false offline transitions, genuine unplug/replug, DHCP
changes, slow/failed echoes, retained cooldowns, failed pool replacement,
bounded uncertain outcomes and separate route dashboard states. Running
processes keep their already imported code; restart with the same command to
load this change. No network settings or applicant database rows are changed by
this fix.

## 2026-10-23: same phone moved to another USB port

The Model Town run was waiting for `enp0s20f0u4`. Kernel and NetworkManager logs show that the same phone reappeared as `enp0s20f0u2` at 11:41:55 and 11:44:16 Asia/Karachi. Its new generic `Wired connection 1` took over the ordinary IPv4 default and DNS, followed by the IPv6 default. The dedicated USB policy profile matched only the old name. Bound Wi-Fi registrations continued, while ordinary application connections could be disrupted. This was a port identity plus OS route/DNS issue, rather than a Private registration category issue.

The prepared fix pins the observed phone by exact USB vendor and serial, independent of port path, randomized MAC or DHCP address. A udev rule assigns the existing `enp0s20f0u4` alias to that phone's tethering NIC before NetworkManager selects a profile. Keeping that alias preserves the registration route key, saved cooldowns, retry affinity and original isolated NetworkManager profile. Other phones are not selected. An absent phone, or a phone in charging/file-transfer mode without tethering, remains offline; Wi-Fi continues.

One-time installation requires root to write `/etc/udev/rules.d`:

```sh
cd /home/ahmad/code/personal/bise_lahore_results && sudo .venv/bin/python -m youthgames_register.usb_setup --config connection_profiles/mobile-usb-device.json --install
```

This command validates the existing isolated profile (both IP families, device-scoped routing rules, ignored USB auto-DNS, no static USB DNS, no changing-MAC pin), verifies rule syntax and publishes without overwriting a different device rule. It reloads udev rules, including on an idempotent rerun after interrupted installation. It does not restart NetworkManager, modify Wi-Fi, activate connections, change live interface names, or access the student database. Unplug/replug the phone and enable USB tethering after installation. The running collector can then detect its original configured alias without restarting.

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register.usb_setup --config connection_profiles/mobile-usb-device.json --status
```

The status command is read-only and distinguishes rule installation, phone presence, actual network-interface creation and profile isolation. `rule_installed: false` means the permanent fix is still pending. Only a physical reconnect can establish live acceptance; syntax checks and simulated tests do not prove that step. Diagnostics/evidence live in `artifacts/usb-stable-identity/`. The application now reports a different USB NIC name when its configured alias is missing, without silently adopting that other route.

Rollback, if required: remove only `/etc/udev/rules.d/70-bise-mobile-usb.rules`, reload udev rules and replug the phone. This returns to port-dependent Linux names; the old isolated profile then works only on its original port. Existing receipts/registrations and Wi-Fi settings are preserved.

[udev device-name assignments and final assignment operator](https://freedesktop.org/software/systemd/man/latest/udev.html); [systemd usb_id property implementation](https://github.com/systemd/systemd/blob/main/src/udev/udev-builtin-usb_id.c).

## Earlier verification: 2026-10-22

NetworkManager logs confirm that at 12:56:08 Asia/Karachi, phone USB interface `enp0s20f0u4` obtained DHCP address `172.31.203.19` and its generated `Wired connection 1` became the default for IPv4 routing and DNS. At 12:56:12 it also became the IPv6 default. At 12:59:17, unplugging the device restored the Wi-Fi defaults. The Wi-Fi interface remained connected throughout this change. The phone's own upstream connectivity during that episode cannot be established from those logs alone.

The generated USB profile no longer existed after unplugging. A persistent USB-only profile was created through NetworkManager's authorized local API:

- Name: `BISE Mobile USB - isolated`
- UUID: `7a929a33-c9b0-51d7-9199-0bc73dd23682`
- Interface: `enp0s20f0u4` (the USB port used in the observed episode)
- Automatic IPv4/IPv6 address configuration, route table `20040`, metric `1200`.
- IPv4 and IPv6 rule priority `5190`, matching only `oif enp0s20f0u4`, lookup table `20040`.
- Automatically supplied USB DNS/search domains are ignored for both IP families.
- Autoconnect enabled, profile preference `100`.

Only sockets explicitly bound to the USB device match these rules. Ordinary laptop traffic keeps its existing main-table Wi-Fi default. The application already binds each route with `SO_BINDTODEVICE`; discovery now searches default routes in all routing tables, including the isolated USB table. DNS resolution continues over the working Wi-Fi connection. This setup expects Wi-Fi to remain connected during parallel processing.

No main Wi-Fi, Docker or Tailscale configuration was changed. No service was restarted and no interface was disconnected or reactivated. The existing reverse-path-filter settings were checked: global `0`, default and Wi-Fi `2` (loose), compatible with the intended multiple uplinks. No sysctl settings were changed.

Physical reconnection was verified on 2026-10-22. Both interfaces have IPv4 addresses, normal default routing and DNS remain on Wi-Fi, and USB has its isolated default in table `20040`. Both bound HTTPS public-IP checks succeeded with distinct egress IPs. Both routes independently fetched the portal registration form with HTTP 200. Before the stable-identity rule above is installed, keep the phone on the **same USB port**. Enable mobile data/USB tethering and keep laptop Wi-Fi connected. A different port may get another interface name and needs the profile binding reviewed before use.

Two application issues were also corrected during this verification. The USB driver reports `UNKNOWN` operational state despite `UP` and `LOWER_UP` carrier flags; discovery now accepts that combination. The installed HTTPCore AnyIO backend applies `socket_options` after TCP connect, which was too late for device-based routing. The explicit-interface transport now sets `SO_BINDTODEVICE` and the source address before connecting, using public HTTPX/HTTPCore/AnyIO interfaces. Binding failures never fall back to another route.

No applicant POST was sent during these network checks. The registration DB contained two earlier uncertain submissions (jobs `5232` and `5233`, interrupted after dispatch). Their receipts are intact but contain no captured server response; they were not reset or resubmitted during verification. Audit and coherent DB backup references are in `artifacts/dual-network/20261022T085946Z/`.

In a later operator-requested one-POST duplicate probe, the portal explicitly rejected a known registered BForm; the original registered job and tracking remained unchanged. The optional `--retry-uncertain` mode can now authorize a fresh attempt on the next run while retaining uncertain history. Without this flag, the reconciliation guard remains enabled. See [the retry contract and captured evidence](MULTIPLE_CONNECTIONS.md#explicit-retry-of-uncertain-submissions).

After reconnection, inspect both routes without submitting forms:

This standalone command records diagnostic IP pins. Registration itself now accepts naturally changed public IPs on these configured interfaces without requiring this command again. Startup waits/retries temporary network outages, stable interface identities retain old pacing, and DHCP sources refresh before reconnecting. See [dynamic-IP recovery](MULTIPLE_CONNECTIONS.md#connect-and-verify).

```sh
cd /home/ahmad/code/personal/bise_lahore_results && .venv/bin/python -m youthgames_register.connections discover --output connection_profiles/wifi-usb.json && .venv/bin/python -m youthgames_register.connections check --config connection_profiles/wifi-usb.json --pin
```

The local audit and scoped rollback are in `artifacts/network-fix/20261022T080538Z/`: `before.json`, `after.json`, `apply-argv.json`, `apply-result.json`, and `rollback.sh`. Rollback removes only this newly created USB profile and is safe to repeat. It should be used with the USB disconnected; it does not touch the Wi-Fi profile or registration data.

Sources: [NetworkManager route-table/rule settings](https://networkmanager.dev/docs/api/latest/nm-settings-nmcli.html), [ignoring automatic DNS](https://networkmanager.dev/docs/api/latest/settings-ipv4.html).
