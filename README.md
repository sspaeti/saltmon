# salt-network-scanner

Live per-device bandwidth for the Salt Fibre Box X6 (Sagemcom F@st 5688T), read
from the router's local JSON-RPC API (`/cgi/json-req`), which the web GUI itself
uses. No firmware changes, read-only.

## Setup

```bash
cp .env.example .env   # set SALT_PASSWORD = router admin password
make                   # live TUI
make log               # live TUI + history to out/samples.jsonl
make once              # one table snapshot (also appends to the log)
make report            # DuckDB aggregations over the log  (uv run report.py --since 24h)
make duck              # duckdb shell with views: samples, rows, summary
make names             # edit names.json (MAC -> friendly name)
```

TUI keys: `q` quit · `s` cycle sort (▼ marks the sorted column) · `n` rename selected device
· `k` kick selected device off Wi-Fi for N minutes · `u` unblock · `a` also show offline hosts
· `g` guest only · `w` Wi-Fi only · `+`/`-` poll interval.

## Kick / temporary block

`k` adds the device's MAC to the router's Wi-Fi MAC-filter blacklist on all four
SSIDs (main + guest, both bands) and removes it again when the time is up (also
on next TUI start, or `make unblock`). State lives in `out/blocks.json`.
CLI: `uv run saltmon.py --block <mac> 60`, `--unblock <mac>`, `--unblock-all`.

**Caveat, measured on this firmware:** every MAC-filter change restarts the
affected Wi-Fi radio, so all Wi-Fi clients on that band drop for ~5–10 s at block
start and at expiry (the repeater uplink needs longer to come back). The filter
mode therefore stays at BLACKLIST permanently (empty list = nobody blocked); the
one-time `--acl-init` has already been run on this box. Devices behind the
repeater or Deco cannot be blocked individually, only the repeater itself.

Activity column: `●` ≥ 0.5 Mb/s, `◐` trickle, `○` idle, `✕` offline.

`names.json` maps MAC → friendly name; edit by hand or press `n` in the TUI.

## What the router exposes

| Source (xpath)                                   | Granularity                               |
|--------------------------------------------------|-------------------------------------------|
| `Device/WiFi/AccessPoints/*/associated_devices`  | per Wi-Fi client: bytes, RSSI, link rate  |
| `Device/Ethernet/Interfaces`                     | per LAN port (PHY1–4), not per host       |
| `Device/IP/Interfaces`                           | WAN (`IP_DATA`), LAN bridge, guest bridge |
| `Device/Hosts/Hosts`                             | names, IPs, which AP / port a host is on  |
| `Device/QoS`                                     | classifications/queues (WAN egress only)  |

Not measurable individually: hosts behind a wired mesh node, a Wi-Fi repeater, or
a NAT-ing extender. They show as `behind RPT/mesh`; their traffic is counted in
the port / repeater row.

`probe.py` dumps the raw API subtrees to `out/` for exploration.
