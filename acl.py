"""Temporary Wi-Fi blocks ("kick for N minutes") via the Salt box MAC filter.

Uses the per-AccessPoint MACFiltering blacklist of the Sagemcom API: the MAC is
added to all four client SSIDs (main + guest, both bands) and the filter mode is
set to BLACKLIST. Blocks are persisted in out/blocks.json with an expiry so they
are lifted even if the TUI was closed in between (`uv run saltmon.py --unblock-all`
or the next TUI start).

Only MACs recorded in blocks.json are ever touched; entries you added by hand in
the router GUI are left alone.

CAUTION (measured on F@st 5688T firmware, Nov 2026): every change to the MAC
filter, mode or list, restarts the affected radio. All Wi-Fi clients on that
band drop for ~5-10 s (the repeater uplink takes longer to come back). So the
filter mode is left at BLACKLIST permanently (empty list = nobody blocked) and
a block costs one blip at start and one at expiry. Never toggle the mode from
the GUI while blocks are active.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

CLIENT_AP_UIDS = (1, 2, 3, 4)  # PRIV0, PRIV1, AP_GUEST_2G4, AP_GUEST_5G
BLOCKS_FILE = Path(__file__).parent / "out" / "blocks.json"


def _q(x: str) -> str:
    return urllib.parse.quote(x, "/=[]'@\"")


def _ap(uid: int) -> str:
    return f"Device/WiFi/AccessPoints/AccessPoint[@uid='{uid}']/MACFiltering"


def load_blocks() -> dict[str, dict]:
    try:
        return json.loads(BLOCKS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_blocks(blocks: dict[str, dict]) -> None:
    BLOCKS_FILE.parent.mkdir(exist_ok=True)
    BLOCKS_FILE.write_text(json.dumps(blocks, indent=1) + "\n")


class Acl:
    """Thin wrapper over the raw JSON-RPC actions. `client` is a logged-in SagemcomClient."""

    def __init__(self, client) -> None:
        self._client = client
        self._req = client._SagemcomClient__api_request_async  # addChild/deleteChild are not exposed by the lib

    async def _state(self, uid: int) -> tuple[str, list[dict]]:
        v = (await self._client.get_value_by_xpath(_ap(uid)))["mac_filtering"]
        return v["mode"], v["mac_addresses"]

    async def modes(self) -> dict[int, str]:
        return {uid: (await self._state(uid))[0] for uid in CLIENT_AP_UIDS}

    async def ensure_blacklist_mode(self) -> list[int]:
        """One-time setup: switch every client AP to BLACKLIST mode (restarts radios!). Returns APs changed."""
        changed = []
        actions = []
        for uid in CLIENT_AP_UIDS:
            mode, _ = await self._state(uid)
            if mode != "BLACKLIST":
                changed.append(uid)
                actions.append({"id": len(actions), "method": "setValue", "xpath": _q(_ap(uid) + "/Mode"),
                                "parameters": {"value": "BLACKLIST"}})
        if actions:
            await self._req(actions, False)
        return changed

    async def block(self, mac: str, minutes: float, name: str = "") -> None:
        mac = mac.lower()
        # record first: if the router is slow to answer (it restarts the radio) the expiry must still happen
        blocks = load_blocks()
        blocks[mac] = {"until": time.time() + minutes * 60, "name": name}
        save_blocks(blocks)
        actions = []
        for uid in CLIENT_AP_UIDS:
            mode, entries = await self._state(uid)
            if mode != "BLACKLIST":
                raise RuntimeError(f"AP{uid} MAC filter mode is {mode}, run `uv run saltmon.py --acl-init` once first")
            if not any(e["mac_address"].lower() == mac for e in entries):
                actions.append({"id": len(actions), "method": "addChild", "xpath": _q(_ap(uid) + "/MACAddresses"),
                                "parameters": {"value": {"MACAddress": {"MACAddress": mac}}}})
        if actions:
            await self._req(actions, False)

    async def unblock(self, mac: str) -> None:
        mac = mac.lower()
        actions = []
        for uid in CLIENT_AP_UIDS:
            _mode, entries = await self._state(uid)
            for e in entries:
                if e["mac_address"].lower() == mac:
                    actions.append({"id": len(actions), "method": "deleteChild",
                                    "xpath": _q(f"{_ap(uid)}/MACAddresses/MACAddress[@uid='{e['uid']}']")})
        if actions:
            await self._req(actions, False)
        blocks = load_blocks()
        blocks.pop(mac, None)
        save_blocks(blocks)

    async def expire(self) -> list[str]:
        """Lift every block whose time is up. Returns the MACs unblocked."""
        now = time.time()
        done = []
        for mac, b in list(load_blocks().items()):
            if b["until"] <= now:
                await self.unblock(mac)
                done.append(mac)
        return done

    async def unblock_all(self) -> list[str]:
        macs = list(load_blocks())
        for mac in macs:
            await self.unblock(mac)
        return macs
