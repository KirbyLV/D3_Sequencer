"""
D3 Sequencer -- local web app for browsing disguise/D3 media and inserting
layers (and timeline sections) at chosen points, driven by an asset-ID based
file naming convention. See README.md before first run.
"""
from __future__ import annotations

import logging
import os
import time

from flask import Flask, jsonify, render_template, request

import config as configmod
from d3_client import D3Client, D3ConnectionError, D3Error, D3RestError, D3ScriptError
import naming
from timecode_util import add_seconds, normalize_timecode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("d3sequencer")

app = Flask(__name__)


def _code_fingerprint() -> dict:
    """Last-modified time of each of our own source files, as a sanity check
    that the process actually running is using the code currently on disk.

    Why this exists: Flask's debug-mode auto-reloader watches file mtimes and
    only restarts the worker process when it sees one change. Deploys done by
    bulk-extracting a zip over the existing files can produce mtimes the
    reloader doesn't reliably pick up as "changed" (timing/granularity
    quirks), so the server can keep running old code in memory even though
    the files on disk are already fixed -- with no visible symptom other than
    old bugs mysteriously persisting after an update. This is why the
    reloader is now OFF (see `use_reloader=False` below): after any code
    update, fully stop (Ctrl+C) and restart `python3 app.py` rather than
    relying on it to notice. `_code_fingerprint()` / `/api/version` exist so
    you can always confirm, from the browser, exactly which file versions the
    currently-running process loaded at startup."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for fname in ("app.py", "config.py", "d3_client.py", "naming.py"):
        path = os.path.join(here, fname)
        try:
            out[fname] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
        except OSError:
            out[fname] = None
    return out


_STARTUP_FINGERPRINT = _code_fingerprint()


def get_client() -> D3Client:
    cfg = configmod.load_config()
    return D3Client.from_config(cfg)


def error_response(e: Exception):
    if isinstance(e, D3ConnectionError):
        return jsonify({"error": "connection", "message": str(e)}), 502
    if isinstance(e, D3ScriptError):
        return jsonify(
            {
                "error": "script",
                "message": str(e),
                "status": e.status,
                "pythonLog": e.python_log,
                "d3Log": e.d3_log,
            }
        ), 500
    if isinstance(e, D3RestError):
        return jsonify({"error": "rest", "message": str(e), "status": e.status}), 500
    if isinstance(e, D3Error):
        return jsonify({"error": "d3", "message": str(e)}), 500
    if isinstance(e, ValueError):
        return jsonify({"error": "bad_request", "message": str(e)}), 400
    log.exception("Unhandled error")
    return jsonify({"error": "internal", "message": str(e)}), 500


@app.errorhandler(Exception)
def handle_any_error(e):
    return error_response(e)


# -- pages ---------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


# -- config ----------------------------------------------------------------


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(configmod.load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    patch = request.get_json(force=True) or {}
    cfg = configmod.update_config(patch)
    return jsonify(cfg)


@app.route("/api/config/reset_expressions", methods=["POST"])
def api_reset_expressions():
    """Restores the built-in expression templates, discarding any saved
    edits. Needed because a saved config.json's expressions otherwise
    always win over new code defaults (see config.py's module docstring)
    -- so a bug fix to a built-in template doesn't reach an existing
    install without this."""
    cfg = configmod.update_config({"expressions": configmod.default_expressions()})
    return jsonify(cfg)


@app.route("/api/version", methods=["GET"])
def api_version():
    """File mtimes of this running process's own source files, captured once
    at startup. If you just deployed an update and this doesn't show the new
    mtimes, the running process is stale -- stop it (Ctrl+C) and restart
    `python3 app.py`; a page refresh alone never fixes this."""
    return jsonify({"loaded_at_startup": _STARTUP_FINGERPRINT, "current_on_disk": _code_fingerprint()})


@app.route("/api/test_connection", methods=["POST"])
def api_test_connection():
    client = get_client()
    result = client.test_connection()
    return jsonify({"ok": True, "pythonLog": result.python_log, "d3Log": result.d3_log})


# -- discovery / console -----------------------------------------------------


@app.route("/api/discover", methods=["POST"])
def api_discover():
    body = request.get_json(force=True) or {}
    target_expr = body.get("target_expr", "d3").strip()
    if not target_expr:
        raise ValueError("target_expr is required")
    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    result = client.inspect(cfg["expressions"], target_expr)
    return jsonify(
        {
            "target_expr": target_expr,
            "attributes": result.return_value,
            "pythonLog": result.python_log,
            "d3Log": result.d3_log,
        }
    )


@app.route("/api/console", methods=["POST"])
def api_console():
    body = request.get_json(force=True) or {}
    script = body.get("script", "")
    if not script.strip():
        raise ValueError("script is required")
    client = get_client()
    result = client.run_script(script)
    return jsonify(
        {
            "returnValue": result.return_value,
            "pythonLog": result.python_log,
            "d3Log": result.d3_log,
        }
    )


# -- media / content browsing ------------------------------------------------


@app.route("/api/media", methods=["GET"])
def api_media():
    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    records = client.list_media(cfg["expressions"])
    filenames = [r.get("filename", "") for r in records if r.get("filename")]
    raw_by_filename = {r.get("filename"): r for r in records if r.get("filename")}

    groups = naming.group_assets(filenames, raw_by_filename)
    unparsed = naming.unparsed_filenames(filenames)
    mapping_numbers_in_files = naming.distinct_mapping_numbers(filenames)

    return jsonify(
        {
            "asset_groups": {aid: g.to_dict() for aid, g in groups.items()},
            "unparsed_filenames": unparsed,
            "mapping_numbers_in_files": mapping_numbers_in_files,
            "mapping_map": cfg.get("mapping_map", {}),
        }
    )


@app.route("/api/tracks", methods=["GET"])
def api_tracks():
    # REST-confirmed (GET /transport/tracks via d3api.swagger.json) -- no
    # scripting/expression template involved, no "Timeline" concept: Tracks
    # sit directly on the project.
    client = get_client()
    return jsonify(client.list_tracks())


@app.route("/api/sections", methods=["GET"])
def api_sections():
    track_name = request.args.get("track", "")
    if not track_name:
        raise ValueError("track query param is required")
    client = get_client()
    return jsonify(client.list_sections(track_name))


@app.route("/api/mappings", methods=["GET"])
def api_mappings():
    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    return jsonify(client.list_mappings(cfg["expressions"]))


# -- timecode ----------------------------------------------------------------

# Human-readable labels for D3's Timecode class's 6 confirmed clockType
# constants (Timecode.SMPTE23976=0 .. SMPTE30=5) -- see config.py's module
# docstring ("Timecode / NTSC frame rates") for how these were confirmed.
_CLOCK_TYPE_LABELS = {
    0: "23.976 fps",
    1: "24 fps",
    2: "25 fps",
    3: "29.97 fps (NTSC, Non-Drop)",
    4: "29.97 fps (NTSC, Drop-Frame)",
    5: "30 fps",
}


@app.route("/api/timecode_info", methods=["GET"])
def api_timecode_info():
    """The project's current SMPTE timecode display setting, for the
    Insert tab to show next to the Start field (e.g. "29.97 fps (NTSC,
    Non-Drop)") so it's clear what a typed timecode will be interpreted
    against."""
    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    info = client.get_timecode_info(cfg["expressions"])
    clock_type = info.get("clockType")
    info["label"] = _CLOCK_TYPE_LABELS.get(clock_type, "Unknown clock type ({})".format(clock_type))
    return jsonify(info)


@app.route("/api/timecode_preview", methods=["POST"])
def api_timecode_preview():
    """Converts a user-typed timecode (e.g. "1:45:00") to real seconds,
    for a live preview in the UI before Insert is actually clicked. Uses
    the exact same normalize_timecode() + D3 Timecode.fromString() path
    /api/insert uses, so what's previewed is exactly what would happen.

    If `length_seconds` is also given, also reports the exact ruler
    position ("end_normalized") the layer's end would be pinned to --
    computed the same way /api/insert now does (see its comment on
    end_timecode), so you can see it lines up with the next section
    break BEFORE clicking Insert, not after."""
    body = request.get_json(force=True) or {}
    raw = _require(body, "timecode")
    normalized = normalize_timecode(raw)
    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    result = client.timecode_to_seconds(cfg["expressions"], normalized)
    response = {"normalized": normalized, "seconds": result.get("seconds"), "clockType": result.get("clockType")}

    length_seconds = body.get("length_seconds")
    if length_seconds not in (None, ""):
        end_timecode = add_seconds(normalized, round(float(length_seconds)))
        end_result = client.timecode_to_seconds(cfg["expressions"], end_timecode)
        response["end_normalized"] = end_timecode
        response["end_seconds"] = end_result.get("seconds")

    return jsonify(response)


@app.route("/api/timecode_add", methods=["POST"])
def api_timecode_add():
    """Advances a timecode by a whole number of seconds of pure clock/
    ruler arithmetic -- see timecode_util.add_seconds()'s docstring for
    why this deliberately does NOT go through D3 at all. Backs the
    Insert tab's "+15s" button, which lets you advance the Start field
    straight to the next slot after an insert without retyping it."""
    body = request.get_json(force=True) or {}
    raw = _require(body, "timecode")
    delta_seconds = int(body.get("delta_seconds", 15))
    return jsonify({"result": add_seconds(raw, delta_seconds)})


# -- insert ------------------------------------------------------------------


VALID_MODES = {"Normal", "Locked"}
VALID_END_MODES = {"loop", "ping-pong", "pause"}


@app.route("/api/insert", methods=["POST"])
def api_insert():
    body = request.get_json(force=True) or {}

    track_name = _require(body, "track_name")
    asset_id = _require(body, "asset_id")
    length_seconds = float(body.get("length_seconds", 8.0))
    mode = body.get("mode", "Normal")
    end_mode = body.get("end_mode", "loop")
    create_section = bool(body.get("create_section", False))
    # Which mapping numbers (from the asset group) to insert a layer for.
    # Omit / pass "all" to insert every mapping number present for this asset.
    requested_mappings = body.get("mapping_numbers", "all")

    if mode not in VALID_MODES:
        raise ValueError("mode must be one of {}".format(sorted(VALID_MODES)))
    if end_mode not in VALID_END_MODES:
        raise ValueError("end_mode must be one of {}".format(sorted(VALID_END_MODES)))

    cfg = configmod.load_config()
    client = D3Client.from_config(cfg)
    mapping_map = cfg.get("mapping_map", {})

    # Start position: prefer a human timecode (e.g. "1:45:00", read straight
    # off the timeline ruler) converted to real seconds via D3's own
    # Timecode class + the project's live SMPTE clock type -- this is what
    # makes NTSC/drop-frame timelines land exactly where the ruler number
    # says, instead of where naive decimal seconds would put them (see
    # config.py's module docstring, "Timecode / NTSC frame rates", for the
    # live data this was validated against). Falls back to raw decimal
    # start_seconds for older callers (e.g. the Discovery Console, or a
    # script) that still want to pass that directly.
    end_timecode = None
    start_timecode_raw = body.get("start_timecode")
    if start_timecode_raw not in (None, ""):
        normalized_start_tc = normalize_timecode(start_timecode_raw)
        start_result = client.timecode_to_seconds(cfg["expressions"], normalized_start_tc)
        start_seconds = start_result["seconds"]

        # The layer's END must land on the same ruler-frame grid as
        # anything else placed via a ruler timecode -- a Section marker,
        # or the next insert's Start after clicking "+15s". Simply adding
        # length_seconds real seconds to start_seconds and converting
        # ONCE does not guarantee that: on an NTSC clock type, N real
        # seconds is essentially never a whole number of frames (15
        # real seconds is 449.55 frames at 29.97fps), so the exact
        # rounding result depends on the start position's own
        # fractional-frame offset and can land a frame off from where a
        # ruler-placed marker "N seconds later" would actually sit --
        # this is the reported bug (a 15-second insert ending a frame
        # before the next section break). Fixed the same way Josh
        # suggested: do the identical ruler-timecode math for the end
        # point. Advance the Start timecode by length_seconds whole
        # ruler seconds (timecode_util.add_seconds -- the same pure
        # clock arithmetic the "+15s" button uses) and run THAT through
        # D3's own Timecode.fromString exactly like Start is, so start
        # and end are each independently frame-exact on the ruler
        # instead of one being derived from the other by real-seconds
        # arithmetic. Sub-second lengths get rounded to the nearest
        # whole ruler second first -- a fractional ruler-second isn't a
        # well-defined position without frame-level math, and Josh's
        # standard insert length (15s) is already a whole number.
        end_timecode = add_seconds(normalized_start_tc, round(length_seconds))
        end_result = client.timecode_to_seconds(cfg["expressions"], end_timecode)
        end_seconds = end_result["seconds"]
        length_seconds = end_seconds - start_seconds
    else:
        start_seconds = float(_require(body, "start_seconds"))

    # Re-fetch + group media so we know exactly which file to load per
    # mapping number (always the latest version) and the asset description.
    records = client.list_media(cfg["expressions"])
    filenames = [r.get("filename", "") for r in records if r.get("filename")]
    raw_by_filename = {r.get("filename"): r for r in records if r.get("filename")}
    groups = naming.group_assets(filenames, raw_by_filename)

    group = groups.get(str(asset_id))
    if group is None:
        raise ValueError("No media found for asset id {!r}".format(asset_id))

    mapping_numbers = group.mapping_numbers() if requested_mappings == "all" else list(requested_mappings)

    missing_config = [mn for mn in mapping_numbers if mn not in mapping_map or not mapping_map[mn]]
    if missing_config:
        raise ValueError(
            "Mapping number(s) {} for asset {} aren't configured yet -- set them up on the Setup page.".format(
                missing_config, asset_id
            )
        )

    results = []
    for mn in mapping_numbers:
        asset = group.latest(mn)
        if asset is None:
            results.append({"mapping_no": mn, "ok": False, "error": "no file found for this mapping number"})
            continue
        resource_path = (asset.raw or {}).get("resource_path") if asset.raw else None
        if not resource_path:
            results.append(
                {"mapping_no": mn, "ok": False, "error": "media record has no resource_path -- re-check the media_list expression"}
            )
            continue
        layer_name = "{}_{}_{}".format(group.asset_id, group.description, mn)
        try:
            res = client.create_layer(
                cfg["expressions"],
                track_name=track_name,
                start_seconds=start_seconds,
                length_seconds=length_seconds,
                layer_name=layer_name,
                resource_path=resource_path,
                mapping_resource_path=mapping_map[mn],
                mode=mode,
                end_mode=end_mode,
            )
            res["mapping_no"] = mn
            res["filename"] = asset.filename
            results.append(res)
        except D3Error as e:
            results.append({"mapping_no": mn, "ok": False, "error": str(e)})

    section_result = None
    if create_section:
        # Sections have no name/label field on this system -- see
        # config.py's module docstring -- so this just marks a Section at
        # start_seconds, with no name to attach.
        section_result = client.create_section(
            cfg["expressions"],
            track_name=track_name,
            start_seconds=start_seconds,
        )

    all_ok = all(r.get("ok") for r in results) and (section_result is None or section_result.get("ok"))
    return jsonify(
        {
            "ok": all_ok,
            "layers": results,
            "section": section_result,
            # Only present when start_timecode was used -- lets the UI (or
            # you, reading the raw response) confirm exactly what ruler
            # position the layer's end was pinned to.
            "end_timecode": end_timecode,
        }
    )


def _require(body: dict, key: str):
    if key not in body or body[key] in (None, ""):
        raise ValueError("{} is required".format(key))
    return body[key]


if __name__ == "__main__":
    log.info("D3 Sequencer starting. Source file versions loaded: %s", _STARTUP_FINGERPRINT)
    log.info("Check http://127.0.0.1:5151/api/version any time to confirm this process picked up your latest changes.")
    # use_reloader=False is deliberate -- see _code_fingerprint()'s docstring.
    # After deploying a code update, fully stop this process and re-run
    # `python3 app.py`; don't rely on auto-reload to notice the change.
    app.run(host="127.0.0.1", port=5151, debug=True, use_reloader=False)
