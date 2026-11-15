# rackmap

Self-hosted network and rack manager. It is being built in stages:

| Stage | What it adds | State |
|---|---|---|
| 1 | Runs, first-run administrator account, sign-in | done |
| 2 | Equipment database and an editable rack view with click-for-details | next |
| 3 | Network discovery that asks where each device sits | planned |
| 4 | Browser terminals through a console server (Digi CM first) | planned |
| 5 | Server panel: power control, text console, slot for a network KVM | planned |
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

## Data

Everything lives in one SQLite file, `rackmap.db`, in the directory named by `RACKMAP_DATA`
(default `./data`). Back that directory up; delete it to start again.

## Develop

```
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest
```

## Licence

MIT. See `LICENSE`.
