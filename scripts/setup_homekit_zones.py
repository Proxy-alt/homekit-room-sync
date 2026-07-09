#!/usr/bin/env python3
"""Create HomeKit Zones matching Home Assistant Floors (macOS only).

homekit_room_sync already syncs each HA Area to a HomeKit *Room* over HAP
(the HomeKit Accessory Protocol). Apple Home *Zones* (e.g. "Upstairs") are a
level above Rooms, and HAP has no zone characteristic at all -- no bridge or
accessory, including this integration, can ever set one. Zones can only be
created by a HomeKit *controller* app acting with the user's HomeKit
permission (the same way Home.app itself does it).

This script drives HomeClaw (https://github.com/omarshahine/HomeClaw), a
native, MIT-licensed macOS app with the real HomeKit framework entitlement,
to do that part: for each HA Floor, create a matching HomeKit Zone and put
the Rooms (HA Areas) on that floor into it.

IMPORTANT — read before running:

  HomeClaw's own documentation and release notes disagree about which
  room/zone-*creation* commands its CLI actually exposes (as of the version
  available when this script was written, only room *re-assignment* to
  already-existing rooms was confirmed in the changelog). Rather than
  hard-code guessed flags that might just fail silently, this script probes
  `homeclaw-cli --help` at runtime and only calls commands it can actually
  see advertised there. If HomeClaw can't do a step, this script tells you
  exactly what to click in Home.app instead of guessing.

  The most robust way to do this today is actually interactive: install
  HomeClaw, add its MCP server to your Claude Code / Claude Desktop config,
  and ask Claude directly to set up your zones. A live agent can adapt to
  whatever HomeClaw's real tool surface is; this script can't.

Requires (macOS only):
  - HomeClaw installed from the Mac App Store, with `homeclaw-cli` on PATH
  - `pip install websockets`
  - A Home Assistant long-lived access token
    (Profile -> Security -> Long-Lived Access Tokens)

Usage:
  python3 scripts/setup_homekit_zones.py --ha-url http://homeassistant.local:8123 \\
      --ha-token <token>                     # dry run: prints the plan only
  python3 scripts/setup_homekit_zones.py ... --apply                # applies it
  python3 scripts/setup_homekit_zones.py ... --apply --ui-fallback  # + guided
                                                                     #   manual
                                                                     #   assist
                                                                     #   for
                                                                     #   whatever
                                                                     #   HomeClaw
                                                                     #   couldn't
                                                                     #   do

HA_URL and HA_TOKEN can also be set via environment variables or a `.env`
file (see env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover - environment guidance only
    print(
        "This script needs the 'websockets' package: pip install websockets",
        file=sys.stderr,
    )
    sys.exit(1)


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file, if present."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


@dataclass
class FloorPlan:
    """A Zone to create and the Rooms (HA Areas) that belong in it."""

    zone_name: str
    room_names: list[str] = field(default_factory=list)


async def fetch_floor_plans(ha_url: str, ha_token: str) -> list[FloorPlan]:
    """Fetch HA's Floor + Area registries and group Areas under their Floor.

    Uses Home Assistant's authenticated WebSocket API (registries are not
    exposed over the plain REST API): `config/floor_registry/list` and
    `config/area_registry/list`.
    """
    ws_url = ha_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    ws_url += "/api/websocket"

    async with websockets.connect(ws_url) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake from {ws_url}: {hello}")

        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {auth_result}")

        async def _call(msg_id: int, msg_type: str) -> list[dict]:
            await ws.send(json.dumps({"id": msg_id, "type": msg_type}))
            response = json.loads(await ws.recv())
            if not response.get("success"):
                raise RuntimeError(f"{msg_type} failed: {response}")
            return response["result"]

        floors = await _call(1, "config/floor_registry/list")
        areas = await _call(2, "config/area_registry/list")

    floors_by_id = {floor["floor_id"]: floor["name"] for floor in floors}
    plans: dict[str, FloorPlan] = {}
    unfloored_areas: list[str] = []

    for area in areas:
        floor_id = area.get("floor_id")
        area_name = area.get("name") or area["area_id"]
        if not floor_id or floor_id not in floors_by_id:
            unfloored_areas.append(area_name)
            continue
        zone_name = floors_by_id[floor_id]
        plans.setdefault(zone_name, FloorPlan(zone_name=zone_name)).room_names.append(area_name)

    if unfloored_areas:
        print(
            f"Note: {len(unfloored_areas)} Area(s) have no Floor assigned in Home "
            f"Assistant and will be skipped: {', '.join(sorted(unfloored_areas))}",
            file=sys.stderr,
        )

    return sorted(plans.values(), key=lambda p: p.zone_name)


def discover_homeclaw_commands() -> set[str]:
    """Return the set of subcommand-looking tokens homeclaw-cli advertises."""
    if shutil.which("homeclaw-cli") is None:
        raise RuntimeError(
            "homeclaw-cli not found on PATH. Install HomeClaw from the Mac App "
            "Store first: https://apps.apple.com/us/app/homeclaw/id6759682551"
        )
    result = subprocess.run(
        ["homeclaw-cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    help_text = f"{result.stdout}\n{result.stderr}"
    tokens = set()
    for line in help_text.splitlines():
        stripped = line.strip()
        first_word = stripped.split(" ", 1)[0] if stripped else ""
        if first_word.replace("-", "").isalpha():
            tokens.add(first_word)
    return tokens


def _run_homeclaw(*args: str, dry_run: bool) -> bool:
    command = ["homeclaw-cli", *args]
    print(f"  $ {' '.join(command)}")
    if dry_run:
        return True
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        print(f"    failed: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)
        return False
    return True


def apply_via_homeclaw(
    plans: list[FloorPlan], available_commands: set[str], dry_run: bool
) -> list[FloorPlan]:
    """Attempt each step only via commands homeclaw-cli actually advertises.

    Returns the plans (or partial plans) that could NOT be completed this way.
    """
    zone_create_cmd = next(
        (c for c in ("create-zone", "add-zone") if c in available_commands), None
    )
    room_assign_cmd = next(
        (c for c in ("add-room-to-zone", "assign-room-to-zone") if c in available_commands), None
    )

    if zone_create_cmd is None and room_assign_cmd is None:
        print(
            "homeclaw-cli doesn't advertise a zone-creation or room-assignment "
            "command in this version (checked --help output). Skipping "
            "automated steps entirely for all floors.",
            file=sys.stderr,
        )
        return plans

    remaining: list[FloorPlan] = []
    for plan in plans:
        print(f"\nZone: {plan.zone_name}")
        zone_ok = True
        if zone_create_cmd:
            zone_ok = _run_homeclaw(zone_create_cmd, plan.zone_name, dry_run=dry_run)
        else:
            print("  (no known zone-creation command; assuming zone already exists)")

        unassigned_rooms: list[str] = []
        if room_assign_cmd:
            for room in plan.room_names:
                ok = _run_homeclaw(room_assign_cmd, room, plan.zone_name, dry_run=dry_run)
                if not ok:
                    unassigned_rooms.append(room)
        else:
            unassigned_rooms = list(plan.room_names)

        if not zone_ok or unassigned_rooms:
            remaining.append(FloorPlan(zone_name=plan.zone_name, room_names=unassigned_rooms))

    return remaining


def guided_manual_fallback(remaining: list[FloorPlan]) -> None:
    """Bring Home.app to the front and print exact manual steps.

    Deliberately does NOT attempt to simulate clicks inside Home.app: doing
    so via System Events requires Accessibility permission this script can't
    verify is safe, and the exact UI layout/labels have not been validated
    against a live Home.app session. A wrong click in Home.app can rename or
    remove real accessories, so this prints a checklist instead of guessing.
    """
    if not remaining:
        return

    print(
        "\nHomeClaw couldn't complete the following automatically. "
        "Opening Home.app -- finish these by hand:\n"
    )
    for plan in remaining:
        print(f'  Zone "{plan.zone_name}":')
        print(
            '    Home.app -> Rooms tab -> "+" -> "Add Zone..." -> name it '
            f'"{plan.zone_name}" if it does not already exist.'
        )
        for room in plan.room_names:
            print(
                f'    Then add Room "{room}" to that zone (long-press the room, "Assign to Zone").'
            )
        print()

    subprocess.run(["osascript", "-e", 'tell application "Home" to activate'], check=False)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--ha-url", default=os.environ.get("HA_URL"), help="e.g. http://homeassistant.local:8123"
    )
    parser.add_argument(
        "--ha-token", default=os.environ.get("HA_TOKEN"), help="HA long-lived access token"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually run the commands (default: dry run)"
    )
    parser.add_argument(
        "--ui-fallback",
        action="store_true",
        help="Print manual steps (and bring Home.app to front) for anything HomeClaw couldn't do",
    )
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("This script only works on macOS (it drives HomeClaw/Home.app).", file=sys.stderr)
        return 1

    _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    ha_url = args.ha_url or os.environ.get("HA_URL")
    ha_token = args.ha_token or os.environ.get("HA_TOKEN")
    if not ha_url or not ha_token:
        parser.error(
            "--ha-url/--ha-token (or HA_URL/HA_TOKEN in the environment or .env) are required"
        )

    print("Fetching Floors and Areas from Home Assistant...")
    plans = await fetch_floor_plans(ha_url, ha_token)
    if not plans:
        print("No Areas with a Floor assigned were found -- nothing to do.")
        print("Set up Floors in HA: Settings -> Areas, labels & zones -> Floors.")
        return 0

    print(f"\nPlanned zones ({'DRY RUN' if not args.apply else 'APPLYING'}):")
    for plan in plans:
        print(f"  {plan.zone_name}: {', '.join(plan.room_names)}")

    try:
        available = discover_homeclaw_commands()
    except RuntimeError as err:
        print(f"\n{err}", file=sys.stderr)
        if args.ui_fallback:
            guided_manual_fallback(plans)
        return 1

    remaining = apply_via_homeclaw(plans, available, dry_run=not args.apply)

    if args.ui_fallback and args.apply:
        guided_manual_fallback(remaining)
    elif remaining and args.apply:
        print(
            f"\n{len(remaining)} zone(s) need manual follow-up. Re-run with "
            "--ui-fallback to get exact steps, or finish them in Home.app yourself."
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
