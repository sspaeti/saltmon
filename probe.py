"""Probe the Salt Fibre Box X6 (Sagemcom F@st 5688T) JSON-RPC API.

Logs in, dumps interesting subtrees to ./out/*.json so we can see which
per-host traffic counters and QoS knobs the firmware actually exposes.

Usage:  cp .env.example .env  # fill SALT_PASSWORD
        uv run probe.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sagemcom_api.client import SagemcomClient
from sagemcom_api.enums import ApiMode, EncryptionMethod

load_dotenv()
HOST = os.environ.get("SALT_HOST", "192.168.1.1")
USER = os.environ.get("SALT_USER", "admin")
PASSWORD = os.environ.get("SALT_PASSWORD")
OUT = Path(__file__).parent / "out"

XPATHS = [
    "Device/DeviceInfo",
    "Device/Hosts",
    "Device/IP/Interfaces",
    "Device/Ethernet/Interfaces",
    "Device/WiFi/SSIDs",
    "Device/WiFi/AccessPoints",
    "Device/WiFi/Radios",
    "Device/QoS",
    "Device/Services/BandwidthMonitoring",
    "Device/UserInterface",
    "Device/DeviceSummary",
    "Device/Optical",
    "Device/PPP/Interfaces",
]


async def main() -> int:
    if not PASSWORD or PASSWORD == "change-me":
        print("Set SALT_PASSWORD in .env (router admin password).", file=sys.stderr)
        return 2
    OUT.mkdir(exist_ok=True)
    async with SagemcomClient(
        HOST, USER, PASSWORD, EncryptionMethod.SHA512, api_mode=ApiMode.LEGACY
    ) as client:
        await client.login()
        print(f"logged in to {HOST} as {USER}")
        for xpath in XPATHS:
            name = xpath.replace("/", "_")
            try:
                value = await client.get_value_by_xpath(xpath)
            except Exception as exc:  # noqa: BLE001 - we want to see every failure
                print(f"  FAIL {xpath}: {type(exc).__name__}: {exc}")
                continue
            path = OUT / f"{name}.json"
            path.write_text(json.dumps(value, indent=2, default=str))
            print(f"  ok   {xpath} -> {path.relative_to(Path.cwd())} ({path.stat().st_size} B)")

        hosts = await client.get_hosts(only_active=True)
        print(f"\n{len(hosts)} active hosts:")
        for h in hosts:
            print(f"  {h.ip_address or '-':15} {h.phys_address or '-':17} {h.interface_type or '-':10} {h.name or h.user_friendly_name or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
