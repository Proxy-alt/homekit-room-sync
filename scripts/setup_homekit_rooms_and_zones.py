#!/usr/bin/env python3
"""Generate a .command file that sets up Apple Home Rooms and Zones via HomeClaw.

Neither HomeKit Rooms nor Zones can be set by a HomeKit bridge or accessory
(this repo's homekit_room_sync integration, or HA's own `homekit` component,
included). We verified this the hard way: earlier versions of
homekit_room_sync wrote an `entity_config[entity_id]["room"]` key, believing
it would move accessories into matching HomeKit Rooms -- but HA core's
`homekit` component has no "room" key anywhere in its schema (checked
const.py, accessories.py, util.py, __init__.py: zero references). That write
was a silent no-op the whole time, which is why accessories kept showing up
in "Default Room" no matter what. Both Room and Zone assignment are
controller-app-only concepts in real HomeKit (`HMHome.assignAccessory`,
`HMHome.addRoom`, `HMHome.addZone` in Apple's HomeKit framework) -- only an
app with the user's HomeKit permission can do this, the same way Home.app
itself does it.

This script does NOT touch HomeKit itself. It reads your HA Area/Floor
structure and writes a plain, readable `.command` shell script (double-click
in Finder to run, like any other .command file) that drives HomeClaw
(https://github.com/omarshahine/HomeClaw), a native, MIT-licensed macOS app
with the real HomeKit framework entitlement, to:
  1. Create a HomeKit Room per HA Area, and assign each Area's entities
     (matched by friendly_name) into it.
  2. Create a HomeKit Zone per HA Floor, and add each Floor's Areas/Rooms
     into it.

All of the commands below were verified against a real, installed HomeClaw
CLI (`homeclaw-cli --help` / `homeclaw-cli help <subcommand>`), not just its
docs -- which elsewhere in this project turned out to disagree with its own
release notes. `create-room`, `create-zone`, `add-room-to-zone`, and
`assign-rooms` are all real, confirmed subcommands.

IMPORTANT gotchas found while verifying this against a live HomeClaw install:

  - `assign-rooms` reads a JSON file, and HomeClaw's CLI runs inside its own
    App Sandbox container (`com.shahine.homeclaw.cli`) -- in testing, it
    could not read a JSON file from /tmp, ~/Desktop, ~/Documents, or the
    current directory, all with the same "you don't have permission to view
    it" error. If you hit that, grant your terminal app Full Disk Access in
    System Settings -> Privacy & Security -> Full Disk Access.

  - `assign-rooms` matches accessories by exact name (or HomeKit UUID). It
    matches against each entity's current HA friendly_name -- if you've
    renamed an accessory directly in the native HomeKit integration UI (or
    in Home.app), the names may no longer match and that accessory will be
    reported as not found. Always run with --dry-run first and check the
    output for anything it couldn't match.

  - This is simpler (and less robust) than a dedicated tool like haconnect
    (https://github.com/canadianblaken/ha-homekit-bridge-connect), which
    matches HA entities to HomeKit accessories by physically actuating each
    device and watching for the corresponding state change, rather than
    trusting names to still agree. haconnect is very new (no version history
    to speak of yet) so we're not defaulting to it here, but it's worth
    knowing about if name-based matching doesn't work well for you.

Requires (macOS only, to generate the .command file):
  - HomeClaw installed from the Mac App Store, with `homeclaw-cli` on PATH
  - `pip install websockets`
  - A Home Assistant long-lived access token
    (Profile -> Security -> Long-Lived Access Tokens)

Usage:
  python3 scripts/setup_homekit_rooms_and_zones.py \\
      --ha-url http://homeassistant.local:8123 --ha-token <token>
  # -> writes ./homekit_rooms_and_zones_setup.command
  # Review it, then run it with --dry-run first:
  #   ./homekit_rooms_and_zones_setup.command --dry-run
  # and for real once you're happy with the preview:
  #   ./homekit_rooms_and_zones_setup.command

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
from datetime import datetime, timezone
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
class RoomPlan:
    """A Room to create and the accessory names (by HA friendly_name) in it."""

    room_name: str
    accessory_names: list[str] = field(default_factory=list)


@dataclass
class FloorPlan:
    """A Zone to create and the Rooms (HA Areas) that belong in it."""

    zone_name: str
    room_names: list[str] = field(default_factory=list)


async def fetch_plans(ha_url: str, ha_token: str) -> tuple[list[RoomPlan], list[FloorPlan]]:
    """Fetch HA's registries and build Room and Zone plans.

    Uses Home Assistant's authenticated WebSocket API (registries -- and
    friendly names, which live on entity *state*, not the registry -- aren't
    exposed over the plain REST API): `config/floor_registry/list`,
    `config/area_registry/list`, `config/entity_registry/list`,
    `config/device_registry/list`, and `get_states`.

    Mirrors the exact same area-resolution rule the homekit_room_sync
    integration itself uses: an entity's own area, else its device's area.
    """
    normalized = ha_url.rstrip("/")
    if normalized.startswith("https://"):
        ws_url = "wss://" + normalized.removeprefix("https://")
    elif normalized.startswith("http://"):
        ws_url = "ws://" + normalized.removeprefix("http://")
    elif normalized.startswith(("ws://", "wss://")):
        ws_url = normalized
    else:
        # No scheme given (e.g. "homeassistant.local:8123") -- assume http.
        ws_url = "ws://" + normalized
    ws_url += "/api/websocket"

    async with websockets.connect(ws_url) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected handshake from {ws_url}: {hello}")

        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_result = json.loads(await ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {auth_result}")

        async def _call(msg_id: int, msg_type: str) -> list[dict] | dict:
            await ws.send(json.dumps({"id": msg_id, "type": msg_type}))
            response = json.loads(await ws.recv())
            if not response.get("success"):
                raise RuntimeError(f"{msg_type} failed: {response}")
            return response["result"]

        floors = await _call(1, "config/floor_registry/list")
        areas = await _call(2, "config/area_registry/list")
        entities = await _call(3, "config/entity_registry/list")
        devices = await _call(4, "config/device_registry/list")
        states = await _call(5, "get_states")

    floors_by_id = {floor["floor_id"]: floor["name"] for floor in floors}
    areas_by_id = {area["area_id"]: area["name"] for area in areas}
    device_area_by_id = {
        device["id"]: device.get("area_id") for device in devices if device.get("area_id")
    }
    friendly_name_by_entity = {
        state["entity_id"]: state.get("attributes", {}).get("friendly_name") for state in states
    }

    room_plans: dict[str, RoomPlan] = {}
    unresolved_entities: list[str] = []

    for entity in entities:
        entity_id = entity["entity_id"]
        area_id = entity.get("area_id") or device_area_by_id.get(entity.get("device_id"))
        if not area_id or area_id not in areas_by_id:
            continue
        friendly_name = friendly_name_by_entity.get(entity_id)
        if not friendly_name:
            unresolved_entities.append(entity_id)
            continue
        area_name = areas_by_id[area_id]
        room_plans.setdefault(area_name, RoomPlan(room_name=area_name)).accessory_names.append(
            friendly_name
        )

    if unresolved_entities:
        preview = ", ".join(sorted(unresolved_entities)[:10])
        more = " ..." if len(unresolved_entities) > 10 else ""
        print(
            f"Note: {len(unresolved_entities)} entit(y/ies) have an Area but no "
            f"current state (unavailable?), so no accessory name to match: {preview}{more}",
            file=sys.stderr,
        )

    floor_plans: dict[str, FloorPlan] = {}
    unfloored_areas: list[str] = []
    for area in areas:
        floor_id = area.get("floor_id")
        area_name = area.get("name") or area["area_id"]
        if not floor_id or floor_id not in floors_by_id:
            unfloored_areas.append(area_name)
            continue
        zone_name = floors_by_id[floor_id]
        floor_plans.setdefault(zone_name, FloorPlan(zone_name=zone_name)).room_names.append(
            area_name
        )

    if unfloored_areas:
        print(
            f"Note: {len(unfloored_areas)} Area(s) have no Floor assigned in Home "
            f"Assistant and will be skipped for Zones: {', '.join(sorted(unfloored_areas))}",
            file=sys.stderr,
        )

    return (
        sorted(room_plans.values(), key=lambda p: p.room_name),
        sorted(floor_plans.values(), key=lambda p: p.zone_name),
    )


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


def render_command_script(
    room_plans: list[RoomPlan],
    floor_plans: list[FloorPlan],
    available_commands: set[str],
) -> str:
    """Render a plain, readable .command shell script that applies the plans.

    Only ever emits homeclaw-cli commands that were actually seen in its
    `--help` output. Anything it can't do is printed as a manual step
    instead of simulated -- this script never clicks anything in Home.app;
    see the module docstring for why.
    """
    create_room_cmd = "create-room" if "create-room" in available_commands else None
    assign_rooms_cmd = "assign-rooms" if "assign-rooms" in available_commands else None
    zone_create_cmd = next(
        (c for c in ("create-zone", "add-zone") if c in available_commands), None
    )
    room_assign_cmd = next(
        (c for c in ("add-room-to-zone", "assign-room-to-zone") if c in available_commands), None
    )

    def echo(message: str, *, stderr: bool = False) -> str:
        # Always single-quote via shlex.quote so bash never re-interprets
        # $(...), backticks, or $VAR inside an HA Area/Floor/entity name.
        redirect = " >&2" if stderr else ""
        return f"echo {shlex.quote(message)}{redirect}"

    # timezone.utc (not datetime.UTC, py3.11+) so this runs on older Pythons too.
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: UP017
    lines: list[str] = [
        "#!/bin/bash",
        f"# Generated by scripts/setup_homekit_rooms_and_zones.py on {generated_at}",
        "# Creates Apple Home Rooms + Zones matching Home Assistant Areas/Floors, via HomeClaw.",
        "# Every command below is exactly what will run -- review before double-clicking.",
        "# Run with --dry-run first: ./homekit_rooms_and_zones_setup.command --dry-run",
        "set -uo pipefail",
        "",
        'DRY_RUN_FLAG=""',
        'if [ "${1:-}" = "--dry-run" ]; then',
        '  DRY_RUN_FLAG="--dry-run"',
        f"  {echo('Running in --dry-run mode: previewing only, nothing will change.')}",
        "fi",
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

    # --- Rooms ---
    lines.append(echo("== Rooms =="))
    if create_room_cmd:
        for plan in room_plans:
            room_q = shlex.quote(plan.room_name)
            fail_msg = echo(
                f"  could not create room {plan.room_name} "
                "(homeclaw-cli reported an error -- it may already exist)"
            )
            lines.append(f"homeclaw-cli {create_room_cmd} {room_q} $DRY_RUN_FLAG || {fail_msg}")
    else:
        lines.append(
            echo(
                "  no create-room command available in this homeclaw-cli version; "
                "create rooms manually in Home.app first"
            )
        )
        lines.append("manual_needed=$((manual_needed+1))")
    lines.append("")

    if assign_rooms_cmd and room_plans:
        assignments = [
            {"accessory": name, "room": plan.room_name}
            for plan in room_plans
            for name in plan.accessory_names
        ]
        assignments_json = json.dumps(assignments, indent=2)
        permission_hint_1 = echo(
            "  assign-rooms could not read its own JSON file -- HomeClaw's CLI is sandboxed.",
            stderr=True,
        )
        permission_hint_2 = echo(
            "  Grant Full Disk Access to your terminal app in System Settings -> "
            "Privacy & Security -> Full Disk Access, then re-run this.",
            stderr=True,
        )
        lines += [
            "ASSIGN_FILE=$(mktemp -t homekit_room_assignments).json",
            "cat > \"$ASSIGN_FILE\" <<'HOMEKIT_ROOM_ASSIGNMENTS_EOF'",
            assignments_json,
            "HOMEKIT_ROOM_ASSIGNMENTS_EOF",
            f'ASSIGN_OUTPUT=$(homeclaw-cli {assign_rooms_cmd} "$ASSIGN_FILE" $DRY_RUN_FLAG 2>&1)',
            "ASSIGN_STATUS=$?",
            'echo "$ASSIGN_OUTPUT"',
            'if [ "$ASSIGN_STATUS" -ne 0 ]; then',
            "  failures=$((failures+1))",
            '  if echo "$ASSIGN_OUTPUT" | grep -qi "permission to view"; then',
            f"    {permission_hint_1}",
            f"    {permission_hint_2}",
            "  fi",
            "fi",
            'rm -f "$ASSIGN_FILE"',
            "",
        ]
    elif room_plans:
        lines.append(
            echo(
                "  no assign-rooms command available in this homeclaw-cli version; "
                "assign accessories to rooms manually in Home.app"
            )
        )
        lines.append("manual_needed=$((manual_needed+1))")
        lines.append("")

    # --- Zones ---
    lines.append(echo("== Zones =="))
    for plan in floor_plans:
        zone = shlex.quote(plan.zone_name)
        lines.append(echo(f"-- Zone: {plan.zone_name} --"))
        if zone_create_cmd:
            fail_msg = echo(
                f"  could not create zone {plan.zone_name} "
                "(homeclaw-cli reported an error -- it may already exist)"
            )
            lines.append(f"homeclaw-cli {zone_create_cmd} {zone} $DRY_RUN_FLAG || {fail_msg}")
        else:
            lines.append(
                echo(
                    f'  (no zone-creation command available; assuming "{plan.zone_name}" '
                    "already exists)"
                )
            )

        if room_assign_cmd:
            for room in plan.room_names:
                room_q = shlex.quote(room)
                fail_msg = echo(f"  failed: {room} -> {plan.zone_name}", stderr=True)
                lines.append(
                    f"homeclaw-cli {room_assign_cmd} {room_q} {zone} $DRY_RUN_FLAG "
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
        '  echo \'  Rooms tab -> "+" -> "Add Room..." / "Add Zone..." if either is missing.\'',
        "  echo '  Long-press an accessory -> \"Move Accessory\" -> pick its room.'",
        "  echo '  Long-press a room -> \"Assign to Zone\" to add it to a zone.'",
        "  open -a Home",
        "fi",
        "",
        'echo ""',
        'echo "Done. $failures command failure(s), $manual_needed step(s) needing manual setup."',
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
        default=Path.cwd() / "homekit_rooms_and_zones_setup.command",
        help="Where to write the generated .command file "
        "(default: ./homekit_rooms_and_zones_setup.command)",
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

    print("Fetching Areas, Floors, and entity names from Home Assistant...")
    room_plans, floor_plans = await fetch_plans(ha_url, ha_token)
    if not room_plans and not floor_plans:
        print("No Areas found -- nothing to do.")
        return 0

    print("\nPlanned rooms:")
    for plan in room_plans:
        print(f"  {plan.room_name}: {', '.join(plan.accessory_names)}")
    print("\nPlanned zones:")
    for plan in floor_plans:
        print(f"  {plan.zone_name}: {', '.join(plan.room_names)}")

    try:
        available = discover_homeclaw_commands()
    except RuntimeError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1

    script_text = render_command_script(room_plans, floor_plans, available)
    args.output.write_text(script_text)
    args.output.chmod(0o755)

    print(f"\nWrote {args.output}")
    print("Review it -- it's a plain shell script, every command it will run is right there.")
    print(f"Preview first:  {args.output} --dry-run")
    print(f"Then apply:     {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
