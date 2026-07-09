#!/usr/bin/env python3
"""Generate a .command file that sets up Apple Home Zones via HomeClaw.

homekit_room_sync already syncs each HA Area to a HomeKit *Room* over HAP
(the HomeKit Accessory Protocol). Apple Home *Zones* (e.g. "Upstairs") are a
level above Rooms, and HAP has no zone characteristic at all -- no bridge or
accessory, including this integration, can ever set one. Zones can only be
created by a HomeKit *controller* app acting with the user's HomeKit
permission (the same way Home.app itself does it).

This script does NOT touch HomeKit itself. It reads your HA Floor/Area
structure and writes a plain, readable `.command` shell script (double-click
in Finder to run, like any other .command file) that drives HomeClaw
(https://github.com/omarshahine/HomeClaw), a native, MIT-licensed macOS app
with the real HomeKit framework entitlement, to create a matching Zone per
Floor and put each Floor's Rooms (HA Areas) into it.

Splitting it this way means you can actually read every command before
anything runs, instead of trusting a Python script's internal subprocess
calls against your live smart home.

IMPORTANT — read before running the generated file:

  HomeClaw's own documentation and release notes disagree about which
  room/zone-*creation* commands its CLI actually exposes (as of the version
  available when this script was written, only room *re-assignment* to
  already-existing rooms was confirmed in the changelog). Rather than
  hard-code guessed flags that might just fail silently, this script probes
  `homeclaw-cli --help` (on the machine generating the file) and only writes
  commands it can actually see advertised there. Whatever it can't do is
  written into the generated script as a printed manual step (plus opening
  Home.app), not simulated -- clicking blindly through Home.app's UI would
  need Accessibility permission and unverified UI selectors that could
  misconfigure a real accessory if wrong.

  The most robust way to do this today is actually interactive: install
  HomeClaw, add its MCP server to your Claude Code / Claude Desktop config,
  and ask Claude directly to set up your zones. A live agent can adapt to
  whatever HomeClaw's real tool surface is; this generated script can't.

Requires (macOS only, to generate the .command file):
  - HomeClaw installed from the Mac App Store, with `homeclaw-cli` on PATH
  - `pip install websockets`
  - A Home Assistant long-lived access token
    (Profile -> Security -> Long-Lived Access Tokens)

Usage:
  python3 scripts/setup_homekit_zones.py --ha-url http://homeassistant.local:8123 \\
      --ha-token <token>
  # -> writes ./homekit_zones_setup.command
  # Review it, then double-click in Finder (or `./homekit_zones_setup.command`)
  # to actually apply it.

HA_URL and HA_TOKEN can also be set via environment variables or a `.env`
file (see env.example). Use --output/-o to change where the .command file
is written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


def render_command_script(plans: list[FloorPlan], available_commands: set[str]) -> str:
    """Render a plain, readable .command shell script that applies `plans`.

    Only ever emits homeclaw-cli commands that were actually seen in its
    `--help` output. Anything it can't do is written as a printed manual
    step (plus opening Home.app at the end) rather than simulated -- this
    script never clicks anything in Home.app itself; see the module
    docstring for why.
    """
    zone_create_cmd = next(
        (c for c in ("create-zone", "add-zone") if c in available_commands), None
    )
    room_assign_cmd = next(
        (c for c in ("add-room-to-zone", "assign-room-to-zone") if c in available_commands), None
    )

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "#!/bin/bash",
        f"# Generated by scripts/setup_homekit_zones.py on {generated_at}",
        "# Creates Apple Home Zones matching your Home Assistant Floors, via HomeClaw.",
        "# Every command below is exactly what will run -- review before double-clicking.",
        "set -uo pipefail",
        "",
        "if ! command -v homeclaw-cli >/dev/null 2>&1; then",
        '  echo "homeclaw-cli not found on PATH. Install HomeClaw from the Mac App Store:" >&2',
        '  echo "  https://apps.apple.com/us/app/homeclaw/id6759682551" >&2',
        "  exit 1",
        "fi",
        "",
        "failures=0",
        "manual_needed=0",
        "",
    ]

    def echo(message: str, *, stderr: bool = False) -> str:
        # Always single-quote via shlex.quote so bash never re-interprets
        # $(...), backticks, or $VAR inside a Floor/Area name pulled from HA.
        redirect = " >&2" if stderr else ""
        return f"echo {shlex.quote(message)}{redirect}"

    for plan in plans:
        zone = shlex.quote(plan.zone_name)
        lines.append(echo(f"== Zone: {plan.zone_name} =="))
        if zone_create_cmd:
            fail_msg = echo(
                f"  could not create zone {plan.zone_name} "
                "(homeclaw-cli reported an error -- it may already exist)"
            )
            lines.append(f"homeclaw-cli {zone_create_cmd} {zone} || {fail_msg}")
        else:
            lines.append(
                echo(
                    f'  (no zone-creation command available; assuming "{plan.zone_name}" already exists)'
                )
            )

        if room_assign_cmd:
            for room in plan.room_names:
                room_q = shlex.quote(room)
                fail_msg = echo(f"  failed: {room} -> {plan.zone_name}", stderr=True)
                lines.append(
                    f"homeclaw-cli {room_assign_cmd} {room_q} {zone} "
                    f"|| {{ {fail_msg}; failures=$((failures+1)); }}"
                )
        else:
            for room in plan.room_names:
                lines.append(
                    echo(
                        f'  MANUAL: add room "{room}" to zone "{plan.zone_name}" '
                        "(no homeclaw-cli command available for this)"
                    )
                )
            lines.append("manual_needed=$((manual_needed+1))")
        lines.append("")

    lines += [
        'if [ "$failures" -gt 0 ] || [ "$manual_needed" -gt 0 ]; then',
        '  echo ""',
        '  echo "Some steps need manual follow-up. In Home.app:"',
        '  echo \'  Rooms tab -> "+" -> "Add Zone..." to create a zone if it is missing.\'',
        "  echo '  Long-press a room -> \"Assign to Zone\" to add it to a zone.'",
        "  open -a Home",
        "fi",
        "",
        'echo ""',
        'echo "Done. $failures command failure(s), $manual_needed zone(s) needing manual setup."',
        "",
    ]
    return "\n".join(lines) + "\n"


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
        "--output",
        "-o",
        type=Path,
        default=Path.cwd() / "homekit_zones_setup.command",
        help="Where to write the generated .command file (default: ./homekit_zones_setup.command)",
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

    print("\nPlanned zones:")
    for plan in plans:
        print(f"  {plan.zone_name}: {', '.join(plan.room_names)}")

    try:
        available = discover_homeclaw_commands()
    except RuntimeError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1

    script_text = render_command_script(plans, available)
    args.output.write_text(script_text)
    args.output.chmod(0o755)

    print(f"\nWrote {args.output}")
    print("Review it -- it's a plain shell script, every command it will run is right there.")
    print(f"Then double-click it in Finder, or run: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
