# rackmap

Self-hosted network and rack manager. It is being built in stages:

| Stage | What it adds | State |
|---|---|---|
| 1 | Runs, first-run administrator account, sign-in | done |
| 2 | Equipment database and an editable rack view with click-for-details | done |
| 3 | Network discovery that asks where each device sits | done |
| 4 | Browser terminals through a console server (Digi CM first) | done |
| 5 | Server panel: power control, text console, slot for a network KVM | next |
| 6 | Remote access over Tailscale | planned |

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
RACKMAP_DATA=./data .venv/bin/uvicorn rackmap.app:app --host 0.0.0.0 --port 8080
```

Open `http://<host>:8080`. The first visitor creates the administrator account, so do that
straight away, and keep the port off untrusted networks. rackmap serves plain HTTP; put it
behind a reverse proxy or a VPN for anything beyond a trusted management network.

`deploy/rackmap.service` is a systemd unit for a plain install under `/opt/rackmap` with
data in `/var/lib/rackmap`.

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
