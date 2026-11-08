"""Live per-device bandwidth monitor for the Salt Fibre Box X6 (Sagemcom F@st 5688T).

Polls the router's local JSON-RPC API and turns the cumulative byte counters it
exposes (per Wi-Fi client, per Ethernet port, per bridge/WAN interface) into
live Mbit/s figures.

    uv run saltmon.py                 # Textual TUI, refresh every 3 s
    uv run saltmon.py --once          # two samples, print one table, exit
    uv run saltmon.py --log out/samples.jsonl   # also append every poll to JSONL

Keys in the TUI: q quit · s sort · n rename · j/k move · K kick (block N min) · u unblock · a show offline · g guest-only · w wifi-only · +/- interval
"""

from __future__ import annotations

import argparse
import asyncio
import json

import aiohttp
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sagemcom_api.client import SagemcomClient
from sagemcom_api.enums import ApiMode, EncryptionMethod

from acl import Acl, load_blocks

load_dotenv(Path(__file__).parent / ".env")
HOST = os.environ.get("SALT_HOST", "192.168.1.1")
USER = os.environ.get("SALT_USER", "admin")
PASSWORD = os.environ.get("SALT_PASSWORD", "")
NAMES_FILE = Path(__file__).parent / "names.json"  # optional {"aa:bb:...": "Kid 1 iPad"}

XPATHS = {
    "hosts": "Device/Hosts/Hosts",
    "aps": "Device/WiFi/AccessPoints",
    "eth": "Device/Ethernet/Interfaces",
    "ip": "Device/IP/Interfaces",
}

AP_LABEL = {
    "PRIV0": "Main 2.4G",
    "PRIV1": "Main 5G",
    "AP_GUEST_2G4": "GUEST 2.4G",
    "AP_GUEST_5G": "GUEST 5G",
    "AP_BACKHAUL": "Backhaul",
}
ACT_GLYPH = {"busy": "[bold green]●[/]", "trickle": "[yellow]◐[/]", "idle": "[dim]○[/]", "offline": "[dim red]✕[/]", "blocked": "[bold red]⛔[/]"}
IFACE_LABEL = {"IP_DATA": "WAN", "IP_BR_LAN": "LAN bridge", "IP_BR_GUEST": "GUEST bridge"}

_ALIAS_RE = re.compile(r"Alias='([^']+)'")
_PHY_RE = re.compile(r"Interface\[(PHY\d)\]")


# --------------------------------------------------------------------------- data
@dataclass
class Counter:
    down: int  # bytes router -> device/LAN  (download from the device's view)
    up: int  # bytes device/LAN -> router
    extra: dict = field(default_factory=dict)


@dataclass
class Sample:
    ts: float
    wifi: dict[str, Counter]  # mac -> counters (+ ap alias, rssi, link rates)
    ports: dict[str, Counter]  # PHYn -> counters (+ bit rate, status)
    ifaces: dict[str, Counter]  # IP_DATA / IP_BR_LAN / IP_BR_GUEST
    hosts: list[dict]


@dataclass
class Row:
    name: str
    ip: str
    mac: str
    net: str
    down_mbps: float
    up_mbps: float
    total_gb: float
    rssi: int | None
    link: str
    shared: bool = False  # rate is a port total shared by several hosts
    online: bool = True  # router's own "active" flag
    last_change: str = ""  # ISO time the router last saw active flip
    blocked_until: float = 0.0  # epoch; > 0 means we hold a MAC-filter block on it

    @property
    def activity(self) -> str:
        """● busy, ◐ trickle, ○ idle, ✕ offline, ⛔ blocked."""
        if self.blocked_until:
            return "blocked"
        if not self.online:
            return "offline"
        r = self.down_mbps + self.up_mbps
        return "busy" if r >= 0.5 else ("trickle" if r > 0.005 else "idle")


def _as_list(obj, key_hint: str) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and (k == key_hint or len(obj) == 1):
                return v
        for v in obj.values():
            if isinstance(v, list):
                return v
    return []


def _i(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_sample(raw: dict) -> Sample:
    wifi: dict[str, Counter] = {}
    for ap in _as_list(raw["aps"], "access_points"):
        alias = ap.get("alias", "?")
        for dev in ap.get("associated_devices") or []:
            if not dev.get("active"):
                continue
            st = dev.get("stats") or {}
            wifi[dev["mac_address"].lower()] = Counter(
                _i(st.get("bytes_sent")),
                _i(st.get("bytes_received")),
                {
                    "ap": alias,
                    "rssi": dev.get("signal_strength"),
                    "link_down": _i(dev.get("last_data_downlink_rate")),
                    "link_up": _i(dev.get("last_data_uplink_rate")),
                    "std": dev.get("operating_standard"),
                },
            )
    ports: dict[str, Counter] = {}
    for p in _as_list(raw["eth"], "interfaces"):
        st = p.get("stats") or {}
        ports[p.get("alias", "?")] = Counter(
            _i(st.get("bytes_sent")),
            _i(st.get("bytes_received")),
            {"rate": p.get("current_bit_rate"), "status": p.get("status")},
        )
    ifaces: dict[str, Counter] = {}
    for it in _as_list(raw["ip"], "interfaces"):
        st = it.get("stats") or {}
        alias = it.get("alias", "?")
        if alias == "IP_DATA":  # WAN: received = download
            ifaces[alias] = Counter(_i(st.get("bytes_received")), _i(st.get("bytes_sent")))
        else:  # LAN-side bridges: sent = download towards clients
            ifaces[alias] = Counter(_i(st.get("bytes_sent")), _i(st.get("bytes_received")))
    hosts = [h for h in _as_list(raw["hosts"], "hosts") if isinstance(h, dict)]
    return Sample(time.time(), wifi, ports, ifaces, hosts)


def _mbps(new: int, old: int, dt: float) -> float:
    d = new - old
    if d < 0 or dt <= 0:  # counter wrap / reset
        return 0.0
    return d * 8 / dt / 1e6


def _rate(a: Counter | None, b: Counter | None, dt: float) -> tuple[float, float]:
    if a is None or b is None:
        return 0.0, 0.0
    return _mbps(b.down, a.down, dt), _mbps(b.up, a.up, dt)


def load_names() -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in json.loads(NAMES_FILE.read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_name(mac: str, name: str) -> dict[str, str]:
    names = load_names()
    if name.strip():
        names[mac.lower()] = name.strip()
    else:
        names.pop(mac.lower(), None)
    NAMES_FILE.write_text(json.dumps(dict(sorted(names.items())), indent=1, ensure_ascii=False) + "\n")
    return names


def host_name(h: dict, names: dict[str, str]) -> str:
    mac = (h.get("phys_address") or "").lower()
    for cand in (names.get(mac), h.get("user_friendly_name"), h.get("host_name"), h.get("user_host_name")):
        if cand and cand.lower() != mac:
            return cand
    return mac or "?"


def build_rows(prev: Sample, cur: Sample, names: dict[str, str], include_offline: bool = False) -> list[Row]:
    dt = cur.ts - prev.ts
    by_mac = {(h.get("phys_address") or "").lower(): h for h in cur.hosts}
    rows: list[Row] = []
    seen: set[str] = set()

    # Wi-Fi clients: exact per-device counters
    for mac, c in cur.wifi.items():
        h = by_mac.get(mac, {"phys_address": mac})
        down, up = _rate(prev.wifi.get(mac), c, dt)
        ex = c.extra
        rows.append(
            Row(
                host_name(h, names),
                h.get("ip_address") or "",
                mac,
                AP_LABEL.get(ex["ap"], ex["ap"]),
                down,
                up,
                (c.down + c.up) / 1e9,
                ex.get("rssi"),
                f"{ex['link_down'] / 1000:.0f}/{ex['link_up'] / 1000:.0f} {ex.get('std') or ''}".strip(),
            )
        )
        seen.add(mac)

    # Wired hosts: counters exist per port only, so hosts on one port share a figure
    port_hosts: dict[str, list[dict]] = {}
    for h in cur.hosts:
        mac = (h.get("phys_address") or "").lower()
        if mac in seen or not h.get("active"):
            continue
        m = _PHY_RE.search(h.get("layer1_interface") or "")
        if m:
            port_hosts.setdefault(m.group(1), []).append(h)
            seen.add(mac)
    for phy, hs in port_hosts.items():
        c = cur.ports.get(phy)
        down, up = _rate(prev.ports.get(phy), c, dt)
        for h in hs:
            rows.append(
                Row(
                    host_name(h, names),
                    h.get("ip_address") or "",
                    (h.get("phys_address") or "").lower(),
                    f"Wired {phy}",
                    down,
                    up,
                    ((c.down + c.up) / 1e9) if c else 0.0,
                    None,
                    f"{(c.extra.get('rate') or 0)} Mb" if c else "",
                    shared=len(hs) > 1,
                )
            )

    # Everything else active: behind the Deco mesh / repeater / unknown L1
    for h in cur.hosts:
        mac = (h.get("phys_address") or "").lower()
        if mac in seen or not h.get("active"):
            continue
        rows.append(Row(host_name(h, names), h.get("ip_address") or "", mac, "behind RPT/mesh", 0.0, 0.0, 0.0, None, "", shared=True))
        seen.add(mac)
    if include_offline:
        for h in cur.hosts:
            mac = (h.get("phys_address") or "").lower()
            if mac in seen or h.get("active"):
                continue
            rows.append(Row(host_name(h, names), h.get("ip_address") or "", mac, "offline", 0.0, 0.0, 0.0, None, "",
                            online=False, last_change=(h.get("active_last_change") or "")[:16].replace("T", " ")))
            seen.add(mac)
    blocks = load_blocks()
    for r in rows:
        if r.mac in blocks:
            r.blocked_until = blocks[r.mac]["until"]
    for mac, b in blocks.items():
        if mac not in seen:
            rows.append(Row(b.get("name") or names.get(mac) or mac, "", mac, "blocked", 0.0, 0.0, 0.0, None, "", online=False, blocked_until=b["until"]))
    return rows


def summary(prev: Sample, cur: Sample) -> dict[str, tuple[float, float]]:
    dt = cur.ts - prev.ts
    out: dict[str, tuple[float, float]] = {}
    for alias, label in IFACE_LABEL.items():
        out[label] = _rate(prev.ifaces.get(alias), cur.ifaces.get(alias), dt)
    for ap_alias, label in AP_LABEL.items():
        d = u = 0.0
        for mac, c in cur.wifi.items():
            if c.extra["ap"] == ap_alias:
                dd, uu = _rate(prev.wifi.get(mac), c, dt)
                d += dd
                u += uu
        if any(c.extra["ap"] == ap_alias for c in cur.wifi.values()):
            out[f"Wi-Fi {label}"] = (d, u)
    return out


# ------------------------------------------------------------------------- client
class Router:
    def __init__(self) -> None:
        self._client: SagemcomClient | None = None

    async def _connect(self) -> SagemcomClient:
        if self._client is None:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), cookie_jar=aiohttp.CookieJar(unsafe=True))
            self._client = SagemcomClient(HOST, USER, PASSWORD, EncryptionMethod.SHA512, api_mode=ApiMode.LEGACY, session=session)
            await self._client.login()
        return self._client

    async def acl(self) -> Acl:
        return Acl(await self._connect())

    async def sample(self) -> Sample:
        try:
            client = await self._connect()
            raw = await client.get_values_by_xpaths(XPATHS)
        except Exception:
            await self.close()
            raise
        return parse_sample(raw)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.logout()
            except Exception:
                pass
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None


def log_sample(path: Path, prev: Sample, cur: Sample, rows: list[Row]) -> None:
    rec = {
        "ts": cur.ts,
        "summary": summary(prev, cur),
        "rows": [r.__dict__ for r in rows],
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ------------------------------------------------------------------------- once
def _fmt(x: float) -> str:
    return f"{x:7.2f}" if x >= 0.01 else "      ·"


async def run_once(interval: float, log: Path | None) -> int:
    from rich.console import Console
    from rich.table import Table

    router = Router()
    try:
        a = await router.sample()
        await asyncio.sleep(interval)
        b = await router.sample()
    finally:
        await router.close()
    names = load_names()
    rows = sorted(build_rows(a, b, names), key=lambda r: r.down_mbps + r.up_mbps, reverse=True)
    con = Console()
    s = summary(a, b)
    con.print(f"[bold]{HOST}[/]  sampled over {b.ts - a.ts:.1f}s   " + "   ".join(f"{k}: ↓{d:.1f} ↑{u:.1f} Mb/s" for k, (d, u) in s.items()))
    t = Table(show_lines=False)
    t.add_column("", justify="center")
    for col in ("Device", "IP", "Network", "↓ Mb/s", "↑ Mb/s", "Total GB", "RSSI", "Link Mb/s"):
        t.add_column(col, justify="right" if "Mb" in col or "GB" in col or col == "RSSI" else "left")
    for r in rows:
        style = "yellow" if r.net.startswith("GUEST") else ("dim" if r.shared else "")
        t.add_row(
            ACT_GLYPH[r.activity],
            r.name[:28] + ("*" if r.shared else ""),
            r.ip,
            r.net,
            _fmt(r.down_mbps),
            _fmt(r.up_mbps),
            f"{r.total_gb:.2f}",
            str(r.rssi) if r.rssi is not None else "",
            r.link,
            style=style,
        )
    con.print(t)
    con.print("[dim]● ≥0.5 Mb/s  ◐ trickle  ○ idle   * = wired port total shared by several hosts, or host behind mesh/repeater (not individually measurable)[/]")
    if log:
        log_sample(log, a, b, rows)
    return 0


async def run_acl(args) -> int:
    router = Router()
    try:
        acl = await router.acl()
        if args.acl_init:
            changed = await acl.ensure_blacklist_mode()
            print("mode set to BLACKLIST on APs:", changed or "already done")
        if args.block:
            mac, minutes = args.block
            await acl.block(mac, float(minutes), load_names().get(mac.lower(), ""))
            print(f"blocked {mac} for {minutes} min")
        if args.unblock:
            await acl.unblock(args.unblock)
            print(f"unblocked {args.unblock}")
        if args.unblock_all:
            print("unblocked:", ", ".join(await acl.unblock_all()) or "nothing was blocked")
        print("blocks now:", json.dumps(load_blocks()) if load_blocks() else "none")
    finally:
        await router.close()
    return 0


# -------------------------------------------------------------------------- TUI
def run_tui(interval: float, log: Path | None) -> int:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, Input, Label, Static

    SORTS = ("rate", "total", "name", "net", "rssi")
    COLS = [  # (key, label, sort key it represents or None)
        ("act", "", None),
        ("name", "Device", "name"),
        ("ip", "IP", None),
        ("net", "Network", "net"),
        ("down", "↓ Mb/s", "rate"),
        ("up", "↑ Mb/s", None),
        ("total", "Total GB", "total"),
        ("rssi", "RSSI", "rssi"),
        ("link", "Link Mb/s", None),
        ("mac", "MAC", None),
        ("seen", "Last seen", None),
    ]

    class KickScreen(ModalScreen[float | None]):
        CSS = """
        KickScreen { align: center middle; }
        #box { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
        """

        def __init__(self, name: str, mac: str, warning: str) -> None:
            super().__init__()
            self.name_, self.mac, self.warning = name, mac, warning

        def compose(self) -> ComposeResult:
            with Vertical(id="box"):
                yield Label(f"Kick [b]{self.name_}[/] ({self.mac}) off Wi-Fi for how many minutes?")
                if self.warning:
                    yield Label(f"[bold red]{self.warning}[/]")
                yield Label("[yellow]Note: the router restarts its Wi-Fi radios on every filter change, "
                            "so ALL Wi-Fi devices drop for ~5-10 s now and again at expiry.[/]")
                yield Label("[dim]Enter = block, Esc = cancel. Auto-unblocked when time is up.[/]")
                yield Input(value="60", placeholder="minutes", type="number")

        def on_mount(self) -> None:
            self.query_one(Input).focus()

        def on_input_submitted(self, ev: Input.Submitted) -> None:
            try:
                self.dismiss(max(1.0, float(ev.value)))
            except ValueError:
                self.dismiss(None)

        def key_escape(self) -> None:
            self.dismiss(None)

    class RenameScreen(ModalScreen[str | None]):
        CSS = """
        RenameScreen { align: center middle; }
        #box { width: 60; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
        """

        def __init__(self, mac: str, current: str) -> None:
            super().__init__()
            self.mac, self.current = mac, current

        def compose(self) -> ComposeResult:
            with Vertical(id="box"):
                yield Label(f"Name for {self.mac}  (empty = reset, Esc = cancel)")
                yield Input(value=self.current, placeholder="e.g. Daughter 1 iPhone")

        def on_mount(self) -> None:
            self.query_one(Input).focus()

        def on_input_submitted(self, ev: Input.Submitted) -> None:
            self.dismiss(ev.value)

        def key_escape(self) -> None:
            self.dismiss(None)

    class SaltMon(App):
        TITLE = f"saltmon · {HOST}"
        CSS = """
        #summary { height: auto; padding: 0 1; background: $panel; }
        DataTable { height: 1fr; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("s", "cycle_sort", "Sort"),
            Binding("n", "rename", "Name"),
            Binding("K", "kick", "Kick"),
            Binding("j", "cursor_down", "", show=False),
            Binding("k", "cursor_up", "", show=False),
            Binding("u", "unblock", "Unblock"),
            Binding("a", "toggle_offline", "Offline too"),
            Binding("g", "toggle_guest", "Guest only"),
            Binding("w", "toggle_wifi", "Wi-Fi only"),
            Binding("plus,equal", "faster", "+ interval"),
            Binding("minus", "slower", "- interval"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.interval = interval
            self.sort_idx = 0
            self.guest_only = False
            self.wifi_only = False
            self.show_offline = False
            self.prev: Sample | None = None
            self.cur: Sample | None = None
            self.rows: list[Row] = []
            self.router = Router()
            self.names = load_names()
            self.err = ""
            self.last_summary: dict[str, tuple[float, float]] | None = None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("connecting…", id="summary")
            yield DataTable(zebra_stripes=True, cursor_type="row")
            yield Footer()

        async def on_mount(self) -> None:
            self._set_columns()
            self.timer = self.set_interval(self.interval, self.poll)
            self.run_worker(self.poll(), exclusive=True)

        # -- columns / sort -------------------------------------------------
        def _set_columns(self) -> None:
            table = self.query_one(DataTable)
            table.clear(columns=True)
            cur_sort = SORTS[self.sort_idx]
            for key, label, sort_key in COLS:
                marker = " ▼" if sort_key == cur_sort else ""
                table.add_column(f"[b u]{label}{marker}[/]" if marker else label, key=key)
            self.sub_title = f"sorted by {cur_sort} ▼"

        def _sorted(self, rows: list[Row]) -> list[Row]:
            key = SORTS[self.sort_idx]
            if key == "rate":
                return sorted(rows, key=lambda r: (r.online, r.down_mbps + r.up_mbps), reverse=True)
            if key == "total":
                return sorted(rows, key=lambda r: (r.online, r.total_gb), reverse=True)
            if key == "name":
                return sorted(rows, key=lambda r: (not r.online, r.name.lower()))
            if key == "rssi":
                return sorted(rows, key=lambda r: (r.rssi is None, r.rssi or 0))
            return sorted(rows, key=lambda r: (r.net, -(r.down_mbps + r.up_mbps)))

        # -- polling ----------------------------------------------------------
        async def poll(self) -> None:
            try:
                cur = await self.router.sample()
                self.err = ""
            except Exception as exc:  # noqa: BLE001
                self.err = f"{type(exc).__name__}: {exc}"
                self.refresh_summary()
                return
            try:
                for mac in await (await self.router.acl()).expire():
                    self.notify(f"block expired, {mac} unblocked", timeout=4)
            except Exception as exc:  # noqa: BLE001
                self.err = f"unblock failed: {exc}"
            if self.prev is not None:
                self.rows = build_rows(self.prev, cur, self.names, include_offline=True)
                self.last_summary = summary(self.prev, cur)
                self.render_rows()
                self.refresh_summary()
                if log:
                    log_sample(log, self.prev, cur, [r for r in self.rows if r.online])
            self.prev = cur

        def refresh_summary(self) -> None:
            online = [r for r in self.rows if r.online]
            busy = sum(r.activity == "busy" for r in online)
            blocked = [r for r in self.rows if r.blocked_until]
            parts = [
                f"[b]every {self.interval:g}s[/]  sort=[b u]{SORTS[self.sort_idx]}[/]",
            ]
            if blocked:
                parts.append("[bold red]⛔ " + ", ".join(f"{r.name} until {time.strftime('%H:%M', time.localtime(r.blocked_until))}" for r in blocked) + "[/]")
            parts += [
                f"online [b]{len(online)}[/] · busy [green]{busy}[/]" + (f" · offline {len(self.rows) - len(online)}" if self.show_offline else ""),
            ]
            if self.guest_only:
                parts.append("[yellow]GUEST ONLY[/]")
            if self.wifi_only:
                parts.append("[cyan]WI-FI ONLY[/]")
            if self.last_summary:
                parts += [f"{k}: [green]↓{d:6.1f}[/] [magenta]↑{u:5.1f}[/]" for k, (d, u) in self.last_summary.items()]
            if self.err:
                parts.append(f"[red]{self.err}[/]")
            self.query_one("#summary", Static).update("   ".join(parts))

        def render_rows(self) -> None:
            rows = self.rows
            if not self.show_offline:
                rows = [r for r in rows if r.online or r.blocked_until]
            if self.guest_only:
                rows = [r for r in rows if r.net.startswith("GUEST")]
            if self.wifi_only:
                rows = [r for r in rows if not r.net.startswith(("Wired", "behind", "offline"))]
            table = self.query_one(DataTable)
            selected = table.cursor_row
            table.clear()
            for r in self._sorted(rows):
                name = r.name[:30] + ("*" if r.shared else "")
                net = r.net
                if r.blocked_until:
                    name = f"[bold red]{name}[/]"
                    net = f"[red]⛔ until {time.strftime('%H:%M', time.localtime(r.blocked_until))}[/]"
                elif not r.online:
                    name = f"[dim]{name}[/]"
                elif r.net.startswith("GUEST"):
                    name = f"[yellow]{name}[/]"
                elif r.activity == "busy":
                    name = f"[bold]{name}[/]"
                table.add_row(
                    ACT_GLYPH[r.activity],
                    name,
                    r.ip,
                    net,
                    _fmt(r.down_mbps),
                    _fmt(r.up_mbps),
                    f"{r.total_gb:.2f}" if r.online else "",
                    str(r.rssi) if r.rssi is not None else "",
                    r.link,
                    r.mac,
                    r.last_change if not r.online else "now",
                    key=r.mac,
                )
            if 0 <= selected < table.row_count:
                table.move_cursor(row=selected)

        # -- actions ------------------------------------------------------------
        def _restart_timer(self) -> None:
            self.timer.stop()
            self.timer = self.set_interval(self.interval, self.poll)
            self.refresh_summary()

        def action_cycle_sort(self) -> None:
            self.sort_idx = (self.sort_idx + 1) % len(SORTS)
            self._set_columns()
            self.render_rows()
            self.refresh_summary()
            self.notify(f"sorted by {SORTS[self.sort_idx]}", timeout=1.5)

        def action_toggle_guest(self) -> None:
            self.guest_only = not self.guest_only
            self.render_rows()
            self.refresh_summary()

        def action_toggle_wifi(self) -> None:
            self.wifi_only = not self.wifi_only
            self.render_rows()
            self.refresh_summary()

        def action_toggle_offline(self) -> None:
            self.show_offline = not self.show_offline
            self.render_rows()
            self.refresh_summary()

        def action_rename(self) -> None:
            table = self.query_one(DataTable)
            if table.row_count == 0:
                return
            mac = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            current = self.names.get(mac, "")

            def done(value: str | None) -> None:
                if value is None:
                    return
                self.names = save_name(mac, value)
                for r in self.rows:
                    if r.mac == mac:
                        r.name = value.strip() or r.mac
                self.render_rows()
                self.notify(f"{mac} → {value.strip() or '(reset)'}  saved to names.json", timeout=2)

            self.push_screen(RenameScreen(mac, current), done)

        def _selected_row(self) -> Row | None:
            table = self.query_one(DataTable)
            if table.row_count == 0:
                return None
            mac = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            return next((r for r in self.rows if r.mac == mac), None)

        def action_cursor_down(self) -> None:
            self.query_one(DataTable).action_cursor_down()

        def action_cursor_up(self) -> None:
            self.query_one(DataTable).action_cursor_up()

        def action_kick(self) -> None:
            r = self._selected_row()
            if r is None:
                return
            warning = ""
            if r.net.startswith(("Wired", "behind")):
                warning = "Not a direct Wi-Fi client: the Wi-Fi MAC filter cannot block this device."
            elif "repeater" in r.name.lower() or "rpt" in r.name.lower():
                warning = "This is a repeater: blocking it kicks EVERY device behind it (incl. your laptop)."
            if r.blocked_until:
                warning = "Already blocked. Enter extends the block from now."

            async def do_block(minutes: float | None) -> None:
                if minutes is None:
                    return
                try:
                    await (await self.router.acl()).block(r.mac, minutes, r.name)
                    self.notify(f"⛔ {r.name} blocked for {minutes:g} min", severity="warning", timeout=4)
                except Exception as exc:  # noqa: BLE001
                    self.notify(f"block failed: {exc}", severity="error", timeout=6)
                await self.poll()

            self.push_screen(KickScreen(r.name, r.mac, warning), lambda m: self.run_worker(do_block(m), exclusive=False))

        def action_unblock(self) -> None:
            r = self._selected_row()
            if r is None or not r.blocked_until:
                self.notify("select a ⛔ blocked row first", timeout=2)
                return

            async def do_unblock() -> None:
                try:
                    await (await self.router.acl()).unblock(r.mac)
                    self.notify(f"{r.name} unblocked", timeout=3)
                except Exception as exc:  # noqa: BLE001
                    self.notify(f"unblock failed: {exc}", severity="error", timeout=6)
                await self.poll()

            self.run_worker(do_unblock(), exclusive=False)

        def action_faster(self) -> None:
            self.interval = max(1.0, self.interval - 1)
            self._restart_timer()

        def action_slower(self) -> None:
            self.interval = min(60.0, self.interval + 1)
            self._restart_timer()

        async def action_quit(self) -> None:
            await self.router.close()
            self.exit()

    SaltMon().run()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="print one table and exit (no TUI)")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between samples (default 3)")
    ap.add_argument("--log", type=Path, help="append every poll as JSON line to this file")
    ap.add_argument("--block", nargs=2, metavar=("MAC", "MINUTES"), help="kick MAC off Wi-Fi for MINUTES, then exit")
    ap.add_argument("--unblock", metavar="MAC", help="lift the block on MAC, then exit")
    ap.add_argument("--unblock-all", action="store_true", help="lift every block we hold, then exit")
    ap.add_argument("--acl-init", action="store_true", help="one-time: set MAC filter mode BLACKLIST on all SSIDs (restarts Wi-Fi!)")
    args = ap.parse_args()
    if not PASSWORD or PASSWORD == "change-me":
        print("Set SALT_PASSWORD in .env (router admin password).", file=sys.stderr)
        return 2
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
    if args.block or args.unblock or args.unblock_all or args.acl_init:
        return asyncio.run(run_acl(args))
    if args.once:
        return asyncio.run(run_once(args.interval, args.log))
    return run_tui(args.interval, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
