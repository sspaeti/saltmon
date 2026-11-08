-- Views over the JSONL written by `saltmon.py --log`.
-- One line per poll: {ts, summary: {label: [down_mbps, up_mbps]}, rows: [{name, ip, mac, net, down_mbps, up_mbps, total_gb, rssi, link, shared}]}
CREATE OR REPLACE VIEW samples AS
SELECT to_timestamp(ts) AS t, summary, rows
FROM read_json_auto(getvariable('log'), format='newline_delimited', maximum_object_size=4000000);

CREATE OR REPLACE VIEW rows AS
SELECT t, r.name, r.ip, r.mac, r.net, r.down_mbps, r.up_mbps, r.total_gb, r.rssi, r.shared
FROM samples, UNNEST(rows) AS u(r);

CREATE OR REPLACE VIEW summary AS
SELECT t, k AS iface, v[1] AS down_mbps, v[2] AS up_mbps
FROM samples, UNNEST(map_keys(summary::MAP(VARCHAR, DOUBLE[]))) AS mk(k),
     LATERAL (SELECT (summary::MAP(VARCHAR, DOUBLE[]))[k] AS v);
.print "views: samples, rows, summary   e.g.  FROM rows WHERE net LIKE 'GUEST%' ORDER BY down_mbps DESC LIMIT 10;"
