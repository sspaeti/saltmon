"""Aggregations over the JSONL log written by `saltmon.py --log`.

    uv run report.py out/samples.jsonl [--since 24h]
"""

import argparse
import re
import sys
from pathlib import Path

import duckdb
from rich.console import Console
from rich.table import Table

ROWS = """
SELECT to_timestamp(ts)::TIMESTAMP AS t, r.name, r.ip, r.mac, r.net,
       r.down_mbps::DOUBLE AS down_mbps, r.up_mbps::DOUBLE AS up_mbps,
       r.total_gb::DOUBLE AS total_gb, r.rssi, r.shared
FROM read_json_auto('{log}', format='newline_delimited', maximum_object_size=4000000), UNNEST(rows) AS u(r)
WHERE to_timestamp(ts) >= now() - INTERVAL '{interval}'
"""
SUMMARY = """
SELECT to_timestamp(ts)::TIMESTAMP AS t, k AS iface, v[1]::DOUBLE AS down_mbps, v[2]::DOUBLE AS up_mbps
FROM read_json_auto('{log}', format='newline_delimited', maximum_object_size=4000000),
     UNNEST(map_keys(summary::MAP(VARCHAR, DOUBLE[]))) AS mk(k),
     LATERAL (SELECT (summary::MAP(VARCHAR, DOUBLE[]))[k] AS v)
WHERE to_timestamp(ts) >= now() - INTERVAL '{interval}'
"""

REPORTS = [
    ("Coverage", "SELECT count(*) AS polls, min(t) AS first, max(t) AS last, count(DISTINCT mac) AS devices FROM rows"),
    (
        "Per interface: avg / p95 / max Mb/s",
        """SELECT iface, round(avg(down_mbps),2) AS avg_down, round(quantile_cont(down_mbps,0.95),1) AS p95_down,
                  round(max(down_mbps),1) AS max_down, round(avg(up_mbps),2) AS avg_up, round(max(up_mbps),1) AS max_up
           FROM summary GROUP BY iface ORDER BY avg_down DESC""",
    ),
    (
        "Top devices by average download (Mb/s)",
        """SELECT any_value(name) AS name, mac, any_value(net) AS net, round(avg(down_mbps),2) AS avg_down,
                  round(max(down_mbps),1) AS max_down, round(avg(up_mbps),2) AS avg_up, round(max(up_mbps),1) AS max_up,
                  count(*) AS polls
           FROM rows GROUP BY mac ORDER BY avg_down DESC LIMIT 15""",
    ),
    (
        "Per network share of download",
        """SELECT net, round(sum(down_mbps),1) AS sum_down_mbps_polls,
                  round(100*sum(down_mbps)/sum(sum(down_mbps)) OVER (),1) AS pct
           FROM rows GROUP BY net ORDER BY sum_down_mbps_polls DESC""",
    ),
    (
        "Guest network by hour (avg Mb/s)",
        """SELECT date_trunc('hour', t) AS hour, round(avg(down_mbps),2) AS avg_down, round(max(down_mbps),1) AS max_down,
                  round(avg(up_mbps),2) AS avg_up
           FROM summary WHERE iface = 'GUEST bridge' GROUP BY hour ORDER BY hour DESC LIMIT 24""",
    ),
    (
        "WAN by hour (avg / max Mb/s)",
        """SELECT date_trunc('hour', t) AS hour, round(avg(down_mbps),2) AS avg_down, round(max(down_mbps),1) AS max_down,
                  round(avg(up_mbps),2) AS avg_up, round(max(up_mbps),1) AS max_up
           FROM summary WHERE iface = 'WAN' GROUP BY hour ORDER BY hour DESC LIMIT 24""",
    ),
    (
        "Busiest minutes (WAN download)",
        """SELECT date_trunc('minute', t) AS minute, round(avg(down_mbps),1) AS avg_down
           FROM summary WHERE iface = 'WAN' GROUP BY minute ORDER BY avg_down DESC LIMIT 10""",
    ),
    (
        "Weak Wi-Fi clients (avg RSSI, dBm)",
        """SELECT any_value(name) AS name, mac, any_value(net) AS net, round(avg(rssi)) AS avg_rssi, round(min(rssi)) AS min_rssi
           FROM rows WHERE rssi IS NOT NULL GROUP BY mac HAVING avg(rssi) < -65 ORDER BY avg_rssi LIMIT 15""",
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", default="out/samples.jsonl")
    ap.add_argument("--since", default="30d", help="window like 2h, 24h, 7d (default 30d)")
    args = ap.parse_args()
    if not Path(args.log).exists():
        print(f"{args.log} not found. Run `make log` or `make once` first.", file=sys.stderr)
        return 1
    m = re.fullmatch(r"(\d+)([hdm])", args.since)
    if not m:
        print("--since must look like 2h, 24h, 7d, 30m", file=sys.stderr)
        return 2
    interval = f"{m.group(1)} {dict(h='hours', d='days', m='minutes')[m.group(2)]}"
    con = duckdb.connect()
    log = args.log.replace("'", "''")
    con.execute("CREATE VIEW rows AS " + ROWS.format(log=log, interval=interval))
    con.execute("CREATE VIEW summary AS " + SUMMARY.format(log=log, interval=interval))
    console = Console()
    console.print(f"[bold]{args.log}[/]  window: last {args.since}")
    for title, sql in REPORTS:
        rel = con.sql(sql)
        t = Table(title=title, title_justify="left")
        for c in rel.columns:
            t.add_column(c, justify="right" if c not in ("name", "mac", "net", "iface") else "left")
        for row in rel.fetchall():
            t.add_row(*[(f"{v}" if v is not None else "") for v in row])
        console.print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
