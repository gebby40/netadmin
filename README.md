# rackmap

Self-hosted network and rack manager. It is being built in stages:

| Stage | What it adds | State |
|---|---|---|
| 1 | Runs, first-run administrator account, sign-in | done |
| 2 | Equipment database and an editable rack view with click-for-details | done |
| 3 | Network discovery that asks where each device sits | done |
| 4 | Browser terminals through a console server (Digi CM first) | done |
| 5 | Server panel (power, health, text console, KVM link) and live up/down status | done |
| 6 | Health, alerts and graphs from a monitoring system (LibreNMS first) | first part done |
| 7 | Remote access over Tailscale | next |

Nothing about any particular network is built in. Devices, addresses and credentials are
entered or discovered through the app and stored in its data directory.

## Run it

With Docker:

```
docker compose up -d
```

Without Docker (Python 3.11 or newer):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
RACKMAP_DATA=./data .venv/bin/python -m rackmap
```

Open `https://<host>:8443`. The first visitor creates the administrator account, so do that
straight away, and keep the port off untrusted networks.

### HTTPS

Console sessions carry passwords, so rackmap serves HTTPS out of the box. With nothing
configured it makes its own certificate on first start. A browser can't verify a home-made
certificate, so it warns the first time; the connection is encrypted either way, and the
certificate's fingerprint is printed in rackmap's log if you want to compare it. Two ways to
get rid of the warning:

- give rackmap a real certificate: set `RACKMAP_TLS_CERT` and `RACKMAP_TLS_KEY` to PEM files;
- put it behind something that already has one (a reverse proxy such as HAProxy, Caddy or
  nginx, or `tailscale serve`) and point that at `https://<host>:8443`. Console terminals use
  WebSockets, so the proxy must pass those through and keep the `Host` header.

| Setting | Default | Meaning |
|---|---|---|
| `RACKMAP_DATA` | `./data` | where the database, keys and cached images live |
| `RACKMAP_HOST` | `0.0.0.0` | address to listen on |
| `RACKMAP_PORT` | `8443` (`8080` with HTTPS off) | port to listen on |
| `RACKMAP_TLS` | `on` | `off` serves plain HTTP; only for a proxy on the same machine |
| `RACKMAP_TLS_CERT`, `RACKMAP_TLS_KEY` | none | your own certificate and key |

`deploy/rackmap.service` is a systemd unit for a plain install under `/opt/rackmap` with
data in `/var/lib/rackmap`.

## Consoles, controllers and KVMs

Three ways to reach a machine when its network is down, each set up from the equipment's
details panel and each invisible until you have one:

- **Console server** (Digi CM / Passport today): mark the equipment as a console server and
  give it an address and sign-in. Every other piece of equipment then gets a *Console port*
  choice, and a terminal to that serial port in the browser.
- **Server controller** (Dell iDRAC, HPE iLO, Supermicro and others, over Redfish): power
  state, health, power buttons and the serial-over-LAN text console.
- **KVM**: mark the equipment as a KVM and say how many ports it has. Every other piece of
  equipment then gets a *KVM port* choice. With a **PiKVM** and an account on it, rackmap shows
  the screen itself (live picture, keyboard, mouse, typing text, and the power and reset
  buttons if the PiKVM's ATX cable is fitted), including the ports of a PiKVM Switch. Any
  other KVM with a web page is linked to; one without a network connection is simply recorded.

Sign-in passwords for these are stored encrypted with a key kept in the data directory, and
rackmap remembers each device's certificate or SSH key the first time and refuses a different
one afterwards.

## Equipment database

rackmap bundles a catalogue of about 14,000 device types (maker, model, height, depth and port
layout) built from the [NetBox community device-type library](https://github.com/netbox-community/devicetype-library),
which is published under CC0 1.0. Photographs of front and rear panels are not bundled: the
first time one is shown, rackmap downloads it from that library and keeps it in the data
directory, so that needs internet access once per model. Without a photograph, or offline,
rackmap draws the panel from the port list. Rebuild the catalogue with `tools/build_catalog.py`.

## Discovery

On the Discovery page, list the private networks to scan (one per line, for example
`192.168.1.0/24`) and press Scan now. For every address that answers, rackmap records its name,
the common services it offers, the title of its web page and, if it speaks SNMP with the
community you give, what it says it is. From that it guesses the kind of device and, where the
model name appears, picks the model from the equipment database. Add… opens the equipment form
with what was found filled in and asks which rack and unit it sits in; Ignore hides a result.
Nothing is added to the inventory on its own.

Hardware (MAC) addresses, and so maker names, are only visible for devices on the same network
as rackmap. To get them for other networks, switch on SNMP on your router and enter its address
and community under "Ask the router".

rackmap refuses to scan public address ranges, and more than 4,096 addresses in one scan.
Run it on a machine that is allowed to reach the networks you want scanned.

## Consoles

On the Consoles page, connect a console server by giving its address and the account rackmap
should sign in with. rackmap signs in once to check the details, remembers the server's SSH
host key, and lists its serial ports. Click a port for a terminal in the browser; label ports
and tie them to equipment so the rack view gets an Open console button.

Supported so far: the Digi CM / Passport family, over SSH (`connect <port>`). Old models that
only offer old SSH algorithms are handled.

Things to know:

- The console server's password is stored encrypted in the database, with the key in
  `secret.key` beside it. That protects a stray copy of the database, not the whole data directory.
- If the server's SSH host key changes, rackmap refuses to connect until you open Edit and
  enter the password again, which accepts the new key.
- Terminals travel over the same connection as the rest of rackmap, which is plain HTTP unless
  you put it behind HTTPS or a VPN. Anything typed in a console, including passwords, is only as
  private as that connection.
- One session per port at a time is the console server's rule, not rackmap's.

## Servers: power, health and text console

Select a server in the rack view and choose Connect controller to attach its management
controller (Dell iDRAC, HPE iLO, Supermicro or Lenovo BMC, and others that speak Redfish). rackmap
then shows whether it is powered on, its health, temperatures and power draw, and offers Power
on, Shut down, Restart and Force off, each behind a confirmation.

- **Text console:** if the controller offers the server's serial console over SSH, enter the
  command it uses (Dell: `console com2`, HPE: `vsp`) and Open text console gives a terminal.
  The server must have serial console redirection switched on for this to show anything.
- **Remote screen:** many controllers only include a graphical remote screen with a paid
  licence. rackmap does not provide one. If you attach a network KVM, save its web address on
  the controller and rackmap links to it.
- The controller's certificate is remembered when it is added; a different certificate later is
  refused until the password is entered again under Edit controller. The password is stored the
  same way as console-server passwords.

## Live status

Equipment with a private IP address is checked about once a minute (a ping, then a few common
ports). The rack view shows a filled dot for answering and a hollow red one for not answering.
Set `RACKMAP_MONITOR=0` to switch this off.

## Monitoring source

If you already run a monitoring system, rackmap can show what it knows on the equipment recorded
here. Open Monitoring, choose Connect a monitoring system, and give its address and an API token.
LibreNMS is supported today; a read-only account's token is enough, and rackmap never changes
anything there.

- **Rack view:** the dot beside each piece of equipment becomes green (healthy), amber (a
  warning is open) or red (an alert is open, or it is not answering the monitoring system).
- **Details panel:** a Health section with what the device reports as, how long it has been
  running, connected ports and traffic, sensor readings (temperatures, fans, power), open
  alerts, day or week graphs and recent log lines.
- **Monitoring page:** everything that currently needs attention, worst first, and the devices
  the monitoring system watches that are not recorded in rackmap yet.

Equipment is matched to monitored devices by IP address, then by name. Under Health, "Monitored
as" sets the match by hand or switches it off for one piece of equipment. rackmap keeps only a
short-lived copy of the answers; the history stays in the monitoring system. The token is stored
the same way as other saved passwords. An HTTPS certificate that can't be verified the usual way
is remembered when the source is connected, and a different one is refused afterwards.

## Data

Everything lives in the directory named by `RACKMAP_DATA` (default `./data`): one SQLite file,
`rackmap.db`, the key for saved passwords, `secret.key`, and downloaded pictures. Back the
directory up and keep it private; delete it to start again.

## Develop

```
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest
```

## Licence

MIT. See `LICENSE`.
