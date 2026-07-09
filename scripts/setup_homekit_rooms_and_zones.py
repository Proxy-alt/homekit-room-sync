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
(https://github.com/omarshahine/HomeClaw -- see the CLI note below), a
native, MIT-licensed macOS app with the real HomeKit framework entitlement,
to:
  1. Create a HomeKit Room per HA Area, and assign each Area's entities
     into it.
  2. Create a HomeKit Zone per HA Floor, and add each Floor's Areas/Rooms
     into it.

Entities are matched to HomeKit accessories primarily by **HAP Serial
Number**, not by display name. Home Assistant's own `homekit` bridge
integration sets each exposed accessory's Serial Number characteristic
(HAP type `00000030-0000-1000-8000-0026BB765291`) to the literal HA
entity_id -- confirmed against live data. That's an exact, unique join key,
unlike display names, which can drift if you've renamed an accessory
directly in Home.app or the native HomeKit integration UI. This script
queries HomeClaw for every accessory's Serial Number at generation time and
resolves to the accessory's HomeKit UUID wherever it matches; it only falls
back to matching by name for accessories where no serial-number match was
found (e.g. ones not bridged through HA's own `homekit` integration at all).

There are two separate, legitimate CLI front-ends to the same HomeClaw app,
and it's easy to mix them up (we did, while building this):
  - `homeclaw-cli`, the native Swift binary bundled inside
    HomeClaw.app itself (`/Applications/HomeClaw.app/Contents/MacOS/`).
    Flat top-level verbs: `create-room`, `create-zone`, `add-room-to-zone
    <room> <zone>`, `assign-rooms <file.json>`.
  - `homekit` (npm package `homekit-cli`, https://github.com/l3wi/homekit-cli),
    a separate, third-party TypeScript CLI that talks to the same running
    HomeClaw app over its socket/MCP interface. Grouped subcommands gated
    behind an explicit `--allow-mutation` flag: `rooms create`, `rooms
    assign <accessory> <room>`, `zones create`, `zones add-room <zone>
    <room>` -- note the argument order is reversed from the native CLI's
    `add-room-to-zone`. Install with `npm i -g homekit-cli`.
Both are real and both work; this script detects whichever is on your PATH
and which command shape it advertises at generation time, and only emits
commands that one actually supports -- it does not hard-code guessed flags.
The grouped CLI's `rooms assign` conveniently takes one accessory at a time
via plain arguments instead of a JSON file, which sidesteps a real
sandboxing bug the native CLI's `assign-rooms` has (see below).

IMPORTANT gotchas found while verifying this against live HomeClaw installs:

  - (native `homeclaw-cli` only) `assign-rooms` reads a JSON file, and its
    CLI runs inside its own App Sandbox container
    (`com.shahine.homeclaw.cli`) -- in testing, it could not read a JSON
    file from /tmp, ~/Desktop, ~/Documents, or the current directory, all
    with the same "you don't have permission to view it" error. If you hit
    that, grant your terminal app Full Disk Access in System Settings ->
    Privacy & Security -> Full Disk Access.

  - Serial-number matching only works for accessories actually bridged
    through HA's own `homekit` integration. Anything else (native HomeKit
    devices, other bridges/hubs) falls back to name matching, which can
    still miss if the accessory's HomeKit name doesn't match its HA
    friendly_name. Always run with --dry-run first and check the output
    for anything it couldn't match.

  - Resolving every accessory's Serial Number means one extra CLI call per
    accessory at generation time (on top of listing them) -- for a home
    with 100+ accessories this can take a while. It's best-effort: if it
    fails outright, generation falls back to name-only matching for
    everything rather than aborting.

  - This is simpler (and less robust for the accessories serial-number
    matching can't resolve) than a dedicated tool like haconnect
    (https://github.com/canadianblaken/ha-homekit-bridge-connect), which
    matches HA entities to HomeKit accessories by physically actuating each
    device and watching for the corresponding state change. haconnect is
    very new (no version history to speak of yet) so we're not defaulting
    to it here, but it's worth knowing about if matching still doesn't work
    well for you.

Requires (macOS only, to generate the .command file):
  - HomeClaw installed from the Mac App Store, with `homekit` or
    `homeclaw-cli` on PATH (see the CLI note above)
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
    """A Room to create and the (entity_id, friendly_name) pairs to place in it."""

    room_name: str
    entities: list[tuple[str, str]] = field(default_factory=list)


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
        room_plans.setdefault(area_name, RoomPlan(room_name=area_name)).entities.append(
            (entity_id, friendly_name)
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


_CLI_BINARY_CANDIDATES = ("homekit", "homeclaw-cli")


def discover_cli_binary() -> str:
    """Return whichever HomeClaw CLI binary name is actually on PATH."""
    for candidate in _CLI_BINARY_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "Neither 'homekit' nor 'homeclaw-cli' found on PATH. Install HomeClaw "
        "from the Mac App Store first: https://apps.apple.com/us/app/homeclaw/id6759682551"
    )


def _help_tokens(binary: str, *args: str) -> set[str]:
    """Return subcommand-looking first-words from `<binary> <args> --help`."""
    result = subprocess.run(
        [binary, *args, "--help"],
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


def discover_top_level_commands(binary: str) -> set[str]:
    """Return the set of subcommand-looking tokens the CLI advertises at its top level."""
    return _help_tokens(binary)


# Standard HAP Accessory Information "Serial Number" characteristic type UUID
# (a protocol constant, not specific to either CLI). HA's own `homekit`
# bridge integration sets this to the literal HA entity_id.
_SERIAL_NUMBER_CHARACTERISTIC_TYPE = "00000030-0000-1000-8000-0026BB765291"


@dataclass
class AccessoryInfo:
    """A HomeKit accessory resolved by HAP Serial Number, and its current room."""

    uuid: str
    current_room: str | None


def _run_cli_json(binary: str, args: list[str]) -> dict | list:
    result = subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{binary} {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return json.loads(result.stdout)


def discover_accessory_info(binary: str, is_grouped: bool) -> dict[str, AccessoryInfo]:
    """Return {serial_number: AccessoryInfo} for every accessory HomeClaw knows about.

    Best-effort: any failure (HomeClaw not reachable, unexpected output,
    timeout) returns {} rather than raising, so callers can fall back to
    name-based matching for everything instead of aborting generation.
    """
    try:
        if is_grouped:
            listing = _run_cli_json(binary, ["accessories", "list", "--format", "json"])
            accessory_ids = [a["id"] for a in listing.get("accessories", [])]
        else:
            listing = _run_cli_json(binary, ["list", "--json"])
            accessory_ids = [a["id"] for a in listing]
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError, KeyError) as err:
        print(
            f"Note: couldn't list HomeKit accessories ({err}); "
            "falling back to name-based matching for all rooms.",
            file=sys.stderr,
        )
        return {}

    total = len(accessory_ids)
    print(f"Resolving {total} accessories by HomeKit serial number...")
    serial_to_info: dict[str, AccessoryInfo] = {}
    for index, accessory_id in enumerate(accessory_ids, start=1):
        if total > 20 and index % 20 == 0:
            print(f"  ...{index}/{total}", file=sys.stderr)
        try:
            if is_grouped:
                detail = _run_cli_json(
                    binary, ["accessories", "get", accessory_id, "--format", "json"]
                )
                accessory = detail.get("accessory", {})
            else:
                accessory = _run_cli_json(binary, ["get", accessory_id, "--json", "--no-refresh"])
        except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
            continue

        current_room = accessory.get("room") or accessory.get("roomName")
        for service in accessory.get("services", []):
            for characteristic in service.get("characteristics", []):
                if characteristic.get("type", "").upper() == _SERIAL_NUMBER_CHARACTERISTIC_TYPE:
                    serial = characteristic.get("value")
                    if serial and serial != "--":
                        serial_to_info[serial] = AccessoryInfo(
                            uuid=accessory_id, current_room=current_room
                        )
                    break

    print(f"  matched {len(serial_to_info)}/{total} accessories by serial number")
    return serial_to_info


def _resolve_identifier(
    entity_id: str, friendly_name: str, serial_to_info: dict[str, AccessoryInfo]
) -> tuple[str, str]:
    """Return (identifier-to-pass-to-the-CLI, human-readable label for messages)."""
    info = serial_to_info.get(entity_id)
    if info:
        return info.uuid, f"{friendly_name} ({entity_id}, matched by serial number)"
    return friendly_name, f"{friendly_name} (matched by name -- no serial-number match found)"


def render_command_script(
    room_plans: list[RoomPlan],
    floor_plans: list[FloorPlan],
    binary: str,
    top_level_commands: set[str],
    serial_to_info: dict[str, AccessoryInfo],
    cleanup_candidates: list[str],
) -> str:
    """Render a plain, readable .command shell script that applies the plans.

    Detects which HomeClaw CLI generation is installed -- the newer grouped
    `rooms`/`zones` subcommands (gated behind --allow-mutation) or the older
    flat top-level verbs -- from `top_level_commands`, and only emits
    commands that generation actually supports; it does not hard-code
    guessed flags. Anything it can't do is printed as a manual step instead
    of simulated -- this script never clicks anything in Home.app; see the
    module docstring for why.

    Each accessory is identified by its HomeKit UUID when `serial_to_info`
    has a match for that entity_id (exact), falling back to its HA
    friendly_name otherwise (best-effort; see module docstring).

    `cleanup_candidates` are room names accessories are being moved *out of*
    this run (their pre-assignment room, from HomeClaw's own data) that
    aren't also one of our own target rooms. If non-empty, the generated
    script re-checks each one live *after* all assignments run and only
    removes ones actually confirmed empty at that point -- never a room
    that's merely "empty" in this plan; that check happens against HomeKit's
    real state, at runtime, right before removal.
    """
    is_grouped = {"rooms", "zones"} <= top_level_commands
    is_flat = {"create-room", "create-zone"} <= top_level_commands

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
        f"if ! command -v {binary} >/dev/null 2>&1; then",
        f'  echo "{binary} not found on PATH. Install HomeClaw from the Mac App Store:" >&2',
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
    if is_grouped:
        for plan in room_plans:
            room_q = shlex.quote(plan.room_name)
            fail_msg = echo(f"  could not create room {plan.room_name} (may already exist)")
            lines.append(
                f"{binary} rooms create {room_q} --allow-mutation $DRY_RUN_FLAG || {fail_msg}"
            )
        lines.append("")
        for plan in room_plans:
            room_q = shlex.quote(plan.room_name)
            for entity_id, friendly_name in plan.entities:
                identifier, label = _resolve_identifier(entity_id, friendly_name, serial_to_info)
                identifier_q = shlex.quote(identifier)
                fail_msg = echo(f"  failed: {label} -> {plan.room_name}", stderr=True)
                lines.append(
                    f"{binary} rooms assign {identifier_q} {room_q} --allow-mutation "
                    f"$DRY_RUN_FLAG || {{ {fail_msg}; failures=$((failures+1)); }}"
                )
    elif is_flat:
        for plan in room_plans:
            room_q = shlex.quote(plan.room_name)
            fail_msg = echo(
                f"  could not create room {plan.room_name} "
                "(homeclaw-cli reported an error -- it may already exist)"
            )
            lines.append(f"{binary} create-room {room_q} $DRY_RUN_FLAG || {fail_msg}")
        lines.append("")

        if room_plans:
            assignments = []
            for plan in room_plans:
                for entity_id, friendly_name in plan.entities:
                    info = serial_to_info.get(entity_id)
                    if info:
                        assignments.append({"uuid": info.uuid, "room": plan.room_name})
                    else:
                        assignments.append({"accessory": friendly_name, "room": plan.room_name})
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
                f'ASSIGN_OUTPUT=$({binary} assign-rooms "$ASSIGN_FILE" $DRY_RUN_FLAG 2>&1)',
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
            ]
    else:
        lines.append(
            echo(
                "  unrecognized homeclaw CLI version; create rooms and assign "
                "accessories manually in Home.app"
            )
        )
        lines.append("manual_needed=$((manual_needed+1))")
    lines.append("")

    # --- Zones ---
    lines.append(echo("== Zones =="))
    for plan in floor_plans:
        zone = shlex.quote(plan.zone_name)
        lines.append(echo(f"-- Zone: {plan.zone_name} --"))
        if is_grouped:
            fail_msg = echo(f"  could not create zone {plan.zone_name} (may already exist)")
            lines.append(
                f"{binary} zones create {zone} --allow-mutation $DRY_RUN_FLAG || {fail_msg}"
            )
            for room in plan.room_names:
                room_q = shlex.quote(room)
                fail_msg = echo(f"  failed: {room} -> {plan.zone_name}", stderr=True)
                # Argument order for this CLI generation is zone, then room.
                lines.append(
                    f"{binary} zones add-room {zone} {room_q} --allow-mutation $DRY_RUN_FLAG "
                    f"|| {{ {fail_msg}; failures=$((failures+1)); }}"
                )
        elif is_flat:
            fail_msg = echo(
                f"  could not create zone {plan.zone_name} "
                "(homeclaw-cli reported an error -- it may already exist)"
            )
            lines.append(f"{binary} create-zone {zone} $DRY_RUN_FLAG || {fail_msg}")
            for room in plan.room_names:
                room_q = shlex.quote(room)
                fail_msg = echo(f"  failed: {room} -> {plan.zone_name}", stderr=True)
                # Argument order for this (older) CLI generation is room, then zone --
                # the opposite of the grouped CLI's `zones add-room <zone> <room>`.
                lines.append(
                    f"{binary} add-room-to-zone {room_q} {zone} $DRY_RUN_FLAG "
                    f"|| {{ {fail_msg}; failures=$((failures+1)); }}"
                )
        else:
            for room in plan.room_names:
                lines.append(
                    echo(
                        f'  MANUAL: add room "{room}" to zone "{plan.zone_name}" '
                        "(unrecognized homeclaw CLI version)"
                    )
                )
            lines.append("manual_needed=$((manual_needed+1))")
        lines.append("")

    # --- Cleanup: remove rooms that ended up empty after the moves above ---
    if cleanup_candidates:
        list_all_cmd = (
            f"{binary} accessories list --format json 2>/dev/null"
            if is_grouped
            else f"{binary} list --json 2>/dev/null"
        )
        # Count client-side from the *full* accessory list rather than trusting
        # the CLI's own --room filter: verified live that it silently returns
        # zero results for multi-word room names (e.g. "Default Room", the
        # single most common HomeKit room name) while the room actually had
        # dozens of accessories in it -- which would have caused this feature
        # to wrongfully delete a room still full of accessories. Fetching once
        # and filtering ourselves (python3 is already a hard requirement to
        # have generated this file at all; no jq dependency needed) sidesteps
        # that entirely. On any parse failure this prints -1, which the caller
        # treats as "unknown, don't touch it" rather than "empty, safe to remove".
        counter = (
            "import json,sys\n"
            "try:\n"
            "    with open(sys.argv[2]) as f:\n"
            "        d = json.load(f)\n"
            "except Exception:\n"
            "    print(-1)\n"
            "else:\n"
            '    items = d if isinstance(d, list) else d.get("accessories", [])\n'
            "    target = sys.argv[1]\n"
            '    n = sum(1 for a in items if (a.get("room") or a.get("roomName")) == target)\n'
            "    print(n)"
        )
        lines.append(echo("== Cleanup: removing now-empty rooms =="))
        lines += [
            "ACCESSORIES_CACHE=$(mktemp -t homekit_accessories_cache).json",
            f'{list_all_cmd} > "$ACCESSORIES_CACHE"',
            "count_accessories_in_room() {",
            f'  python3 -c \'{counter}\' "$1" "$ACCESSORIES_CACHE"',
            "}",
            "",
        ]
        for room_name in cleanup_candidates:
            room_q = shlex.quote(room_name)
            lines.append(f"ROOM_COUNT=$(count_accessories_in_room {room_q})")
            lines.append('if [ "$ROOM_COUNT" = "0" ]; then')
            fail_msg = echo(f"  could not remove empty room {room_name}", stderr=True)
            if is_grouped:
                lines.append(
                    f"  {binary} rooms remove {room_q} --allow-mutation $DRY_RUN_FLAG || {fail_msg}"
                )
            elif is_flat:
                lines.append(f"  {binary} remove-room {room_q} $DRY_RUN_FLAG || {fail_msg}")
            lines.append('elif [ "$ROOM_COUNT" != "-1" ]; then')
            # $ROOM_COUNT must actually expand here, unlike everywhere else `echo()`
            # is used -- so build this one line by hand: the untrusted room name
            # stays single-quoted (safe), $ROOM_COUNT sits in its own
            # double-quoted segment (bash concatenates adjacent quoted strings).
            skip_prefix = shlex.quote(f"  {room_name} still has ")
            skip_suffix = shlex.quote(" accessorie(s); leaving it.")
            lines.append(f'  echo {skip_prefix}"$ROOM_COUNT"{skip_suffix}')
            lines.append("fi")
        lines.append('rm -f "$ACCESSORIES_CACHE"')
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
    parser.add_argument(
        "--delete-empty-rooms",
        action="store_true",
        help="After moving accessories, remove any room they were moved OUT of that ends up "
        "with zero accessories. Only ever removes a room the generated script has just "
        "verified is empty against HomeKit's live state at that point -- never a room "
        "that merely looks empty in this plan, and never one of the target rooms "
        "themselves.",
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
        print(f"  {plan.room_name}: {', '.join(name for _, name in plan.entities)}")
    print("\nPlanned zones:")
    for plan in floor_plans:
        print(f"  {plan.zone_name}: {', '.join(plan.room_names)}")

    try:
        binary = discover_cli_binary()
        top_level_commands = discover_top_level_commands(binary)
    except RuntimeError as err:
        print(f"\n{err}", file=sys.stderr)
        return 1

    is_grouped = {"rooms", "zones"} <= top_level_commands
    print()
    serial_to_info = discover_accessory_info(binary, is_grouped)

    cleanup_candidates: list[str] = []
    if args.delete_empty_rooms:
        target_room_names = {plan.room_name for plan in room_plans}
        source_rooms: set[str] = set()
        for plan in room_plans:
            for entity_id, _friendly_name in plan.entities:
                info = serial_to_info.get(entity_id)
                if info and info.current_room and info.current_room != plan.room_name:
                    source_rooms.add(info.current_room)
        cleanup_candidates = sorted(source_rooms - target_room_names)
        if cleanup_candidates:
            print(
                f"Will check for emptiness after moving accessories: {', '.join(cleanup_candidates)}"
            )

    script_text = render_command_script(
        room_plans, floor_plans, binary, top_level_commands, serial_to_info, cleanup_candidates
    )
    args.output.write_text(script_text)
    args.output.chmod(0o755)

    print(f"\nWrote {args.output}")
    print("Review it -- it's a plain shell script, every command it will run is right there.")
    print(f"Preview first:  {args.output} --dry-run")
    print(f"Then apply:     {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
