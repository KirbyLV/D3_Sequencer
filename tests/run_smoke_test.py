#!/usr/bin/env python3
"""
End-to-end smoke test: starts the mock D3 server, points the Flask app's
config at it, and drives every route through Flask's test client. This
proves the naming parser, config persistence, and Flask<->D3Client wiring
all work correctly using the DEFAULT expression templates -- it cannot (and
does not try to) prove those templates are correct against a *real*
disguise/D3 show, since this sandbox has no network path to one. See
README.md's "Discovery workflow" for how to confirm the real thing.

Run with: python3 tests/run_smoke_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MOCK_PORT = 8765


def wait_for(url, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    failures = []

    def check(label, cond, extra=""):
        status = "PASS" if cond else "FAIL"
        print("[{}] {}{}".format(status, label, (" -- " + extra) if extra and not cond else ""))
        if not cond:
            failures.append(label)

    # 1. start the mock D3 server as a subprocess
    mock_proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tests", "mock_d3_server.py"), str(MOCK_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        ok = wait_for("http://127.0.0.1:{}/api/session/python/execute".format(MOCK_PORT) + "", timeout=10)
        # the above GET will 405, but that still means the server is up; a
        # connection error means it isn't. Treat any HTTP response as "up".
    except Exception:
        pass
    time.sleep(1.0)  # simple settle time, avoids flakiness from wait_for's GET-405 quirk

    # 2. point config at a scratch file + the mock server, then import the app
    tmp_cfg = tempfile.NamedTemporaryFile(prefix="d3seq_test_config_", suffix=".json", delete=False)
    tmp_cfg.close()
    os.environ["D3SEQ_CONFIG_PATH"] = tmp_cfg.name

    import config as configmod  # noqa: E402

    configmod.update_config({"server": {"host": "127.0.0.1", "port": MOCK_PORT, "scheme": "http"}})

    import app as flaskapp  # noqa: E402

    client = flaskapp.app.test_client()

    try:
        # -- timecode normalization is a pure function, no D3/mock involved --
        # exercise its accepted shapes and error cases directly (see
        # timecode_util.py's docstring for the full accepted-shapes list).
        from timecode_util import add_seconds, normalize_timecode  # noqa: E402

        check("normalize bare seconds", normalize_timecode("45") == "00:00:45:00")
        check("normalize MM:SS", normalize_timecode("1:45") == "00:01:45:00")
        check("normalize MM:SS:FF", normalize_timecode("1:45:00") == "00:01:45:00")
        check("normalize MM:SS:FF (Josh's reported value)", normalize_timecode("1:44:27") == "00:01:44:27")
        check("normalize HH:MM:SS:FF", normalize_timecode("0:01:45:00") == "00:01:45:00")
        check(
            "normalize D3's own asString() format ('.' before frames)",
            normalize_timecode("00:01:44.27") == "00:01:44:27",
        )
        check("normalize tolerates surrounding whitespace", normalize_timecode("  1:45:00  ") == "00:01:45:00")
        # Periods as a numeric-keypad-friendly stand-in for colons --
        # Josh's request, since a 10-key keypad has no ':' key.
        check("normalize accepts periods as separators (MM.SS)", normalize_timecode("1.45") == "00:01:45:00")
        check(
            "normalize accepts periods as separators (MM.SS.FF)",
            normalize_timecode("1.45.00") == "00:01:45:00",
        )
        check(
            "normalize accepts periods (Josh's reported value)",
            normalize_timecode("1.44.27") == "00:01:44:27",
        )
        check(
            "normalize accepts a mix of periods and colons",
            normalize_timecode("1.45:00") == "00:01:45:00",
        )
        try:
            normalize_timecode("not a timecode")
            check("normalize rejects garbage", False)
        except ValueError:
            check("normalize rejects garbage", True)
        try:
            normalize_timecode("1:75:00")
            check("normalize rejects seconds >= 60", False)
        except ValueError:
            check("normalize rejects seconds >= 60", True)
        try:
            normalize_timecode("")
            check("normalize rejects empty string", False)
        except ValueError:
            check("normalize rejects empty string", True)

        # -- add_seconds: pure clock arithmetic for the "+15s" advance
        # button (see its docstring for why this is deliberately NOT a
        # round trip through D3's real-seconds conversion).
        check("add_seconds carries minutes on overflow", add_seconds("1:45:00", 15) == "00:02:00:00")
        check("add_seconds preserves the frames field", add_seconds("1:45:27", 15) == "00:02:00:27")
        check("add_seconds accepts period-separated input", add_seconds("1.45.00", 15) == "00:02:00:00")
        check("add_seconds carries hours on overflow", add_seconds("0:59:50:00", 15) == "01:00:05:00")
        check("add_seconds clamps at zero, never negative", add_seconds("0:00:05:00", -100) == "00:00:00:00")

        # -- mock Timecode class matches the exact live-captured data point
        # from Josh's real D3 system (see config.py's module docstring,
        # "Timecode / NTSC frame rates"): a layer placed at 105.0 real
        # seconds on a 29.97fps non-drop (clockType 3) project displays as
        # "1:44:27" -- this IS the bug report that started this feature.
        from mock_d3_server import Timecode as MockTimecode  # noqa: E402

        check(
            "mock Timecode matches live D3: 105.0s @ clockType 3 -> 00:01:44.27",
            MockTimecode(105.0, MockTimecode.SMPTE2997).asString(False) == "00:01:44.27",
            MockTimecode(105.0, MockTimecode.SMPTE2997).asString(False),
        )
        roundtrip_seconds = MockTimecode.fromString("00:01:45:00", MockTimecode.SMPTE2997).t
        check(
            "mock Timecode matches live D3: '1:45:00' @ clockType 3 -> ~105.105s",
            abs(roundtrip_seconds - 105.105) < 0.001,
            roundtrip_seconds,
        )

        # -- config round-trip
        resp = client.get("/api/config")
        check("GET /api/config", resp.status_code == 200)
        cfg = resp.get_json()
        check("config has server.host", cfg["server"]["host"] == "127.0.0.1")

        # -- connection test against the mock
        resp = client.post("/api/test_connection")
        check("POST /api/test_connection", resp.status_code == 200, resp.get_data(as_text=True))

        # -- media listing + naming convention grouping
        resp = client.get("/api/media")
        check("GET /api/media", resp.status_code == 200, resp.get_data(as_text=True))
        media = resp.get_json()
        groups = media["asset_groups"]
        check("asset 042 grouped", "042" in groups)
        check("asset 042 has 3 mapping numbers", set(groups.get("042", {}).get("mapping_numbers", [])) == {"1", "2", "3"})
        latest_042_m3 = groups["042"]["variants"]["3"][0]["filename"] if "042" in groups else None
        check("asset 042 mapping 3 picks newest version (02, not 01)", latest_042_m3 == "042_CityScape_Intro_3_02.mov", str(latest_042_m3))
        latest_042_m3_raw = groups["042"]["variants"]["3"][0]["raw"] if "042" in groups else None
        check(
            "media record carries resource_path (real VideoClip identifier)",
            bool(latest_042_m3_raw and latest_042_m3_raw.get("resource_path")),
            str(latest_042_m3_raw),
        )
        check("unparsed filename surfaced", "not_a_matching_filename.mov" in media["unparsed_filenames"])
        check(
            "distinct mapping numbers found",
            set(media["mapping_numbers_in_files"]) == {"1", "2", "3", "a", "b", "169"},
            str(media["mapping_numbers_in_files"]),
        )

        # -- D3-managed versioning: no version suffix in the reported
        # filename, mapping identifier can be a letter or a plain number
        # (see naming.py's module docstring -- this is a regression test
        # for a real-world report, not just the original spec's form).
        check("asset 1000 (no-version form) grouped", "1000" in groups, str(list(groups.keys())))
        check(
            "asset 1000 has letter mapping identifiers a and b",
            set(groups.get("1000", {}).get("mapping_numbers", [])) == {"a", "b"},
            str(groups.get("1000")),
        )
        asset_1000_a = groups["1000"]["variants"]["a"][0] if "1000" in groups else None
        check(
            "no-version asset has version=None",
            bool(asset_1000_a) and asset_1000_a.get("version") is None,
            str(asset_1000_a),
        )
        check("asset 1020 (numeric letter-form mapping) grouped", "1020" in groups, str(list(groups.keys())))
        check(
            "asset 1020 mapping 169 parsed with no version",
            groups.get("1020", {}).get("mapping_numbers") == ["169"],
            str(groups.get("1020")),
        )

        # -- insert should fail cleanly when mapping numbers aren't configured yet
        # (mapping_map is still empty at this point in the test)
        resp = client.post(
            "/api/insert",
            data=json.dumps(
                {
                    "track_name": "Video 1",
                    "start_seconds": 0,
                    "asset_id": "017",
                    "mapping_numbers": "all",
                }
            ),
            content_type="application/json",
        )
        check("POST /api/insert (unmapped) returns 400", resp.status_code == 400, resp.get_data(as_text=True))

        # -- mappings from D3 (each has a "name" for display and a stable
        # "resource_path" identifier -- see config.py's module docstring)
        resp = client.get("/api/mappings")
        check("GET /api/mappings", resp.status_code == 200, resp.get_data(as_text=True))
        mappings_by_name = {m["name"]: m["resource_path"] for m in resp.get_json()}
        check("mappings include US Screen", "US Screen" in mappings_by_name, str(mappings_by_name))
        check(
            "mapping records carry resource_path",
            all(m.get("resource_path") for m in resp.get_json()),
            str(resp.get_json()),
        )

        # -- mapping config save: mapping_map values are resource_path
        # strings, not display names (see config.py's module docstring).
        # Row "99" is a deliberate typo/accidental extra row, included here
        # specifically to exercise removing it below.
        resp = client.post(
            "/api/config",
            data=json.dumps(
                {
                    "mapping_map": {
                        "1": mappings_by_name["US Screen"],
                        "2": mappings_by_name["SR Screen"],
                        "3": mappings_by_name["SL Screen"],
                        "99": mappings_by_name["Floor"],
                    }
                }
            ),
            content_type="application/json",
        )
        check("POST /api/config mapping_map", resp.status_code == 200)
        resp = client.get("/api/config")
        check("mapping_map row 99 saved", "99" in resp.get_json().get("mapping_map", {}), str(resp.get_json().get("mapping_map")))

        # -- removing a mapping row (the "×" button in the UI) must actually
        # stick, not get silently re-merged back in on the next save. This
        # is a regression test for update_config()'s replace-not-merge
        # behaviour on dict-valued keys -- see config.py's module docstring.
        resp = client.post(
            "/api/config",
            data=json.dumps(
                {
                    "mapping_map": {
                        "1": mappings_by_name["US Screen"],
                        "2": mappings_by_name["SR Screen"],
                        "3": mappings_by_name["SL Screen"],
                    }
                }
            ),
            content_type="application/json",
        )
        check("POST /api/config mapping_map (row 99 removed)", resp.status_code == 200)
        resp = client.get("/api/config")
        saved_map = resp.get_json().get("mapping_map", {})
        check("removed mapping row 99 does not come back", "99" not in saved_map, str(saved_map))
        check("remaining mapping rows 1-3 survived the removal", {"1", "2", "3"} <= set(saved_map.keys()), str(saved_map))

        # -- tracks (REST-confirmed endpoint, no Timeline level -- see README)
        resp = client.get("/api/tracks")
        check("GET /api/tracks", resp.status_code == 200, resp.get_data(as_text=True))
        tracks = {t["name"] for t in resp.get_json()}
        check("tracks include Video 1", "Video 1" in tracks, str(tracks))
        check("track entries carry uid/length (REST schema)", all("uid" in t and "length" in t for t in resp.get_json()))

        # -- sections (REST-confirmed endpoint) -- empty before any insert
        resp = client.get("/api/sections?track=Video 1")
        check("GET /api/sections", resp.status_code == 200, resp.get_data(as_text=True))
        check("no sections yet on Video 1", resp.get_json() == [], resp.get_data(as_text=True))

        # -- insert: single asset, all mapping numbers, with a section
        resp = client.post(
            "/api/insert",
            data=json.dumps(
                {
                    "track_name": "Video 1",
                    "start_seconds": 16,
                    "length_seconds": 8,
                    "mode": "Locked",
                    "end_mode": "loop",
                    "create_section": True,
                    "asset_id": "042",
                    "mapping_numbers": "all",
                }
            ),
            content_type="application/json",
        )
        check("POST /api/insert", resp.status_code == 200, resp.get_data(as_text=True))
        insert_result = resp.get_json()
        check("insert ok=True", insert_result.get("ok") is True, json.dumps(insert_result))
        check("insert created 3 layers", len(insert_result.get("layers", [])) == 3, json.dumps(insert_result))
        check("section created", insert_result.get("section", {}).get("ok") is True, json.dumps(insert_result))
        check(
            "layer used latest version for mapping 3",
            any(l.get("filename") == "042_CityScape_Intro_3_02.mov" for l in insert_result["layers"]),
            json.dumps(insert_result),
        )
        first_layer = insert_result["layers"][0]
        check(
            "insert response includes the exact script sent to D3",
            bool(first_layer.get("_script_sent")) and "resourceManager.load" in first_layer["_script_sent"],
            json.dumps(first_layer),
        )
        check(
            "insert response includes a fresh post-create verification read-back",
            first_layer.get("_verify", {}).get("found") is True and first_layer["_verify"].get("video") is not None,
            json.dumps(first_layer.get("_verify")),
        )

        # -- sections now show the one we just created via the REST-confirmed endpoint
        resp = client.get("/api/sections?track=Video 1")
        check("GET /api/sections after insert", resp.status_code == 200, resp.get_data(as_text=True))
        sections = resp.get_json()
        check("section shows up with correct time", any(s.get("time") == 16 for s in sections), str(sections))

        # -- timecode info: the mock's guisystem defaults to clockType 3
        # (Timecode.SMPTE2997, 29.97fps non-drop), matching Josh's real
        # project -- see mock_d3_server.py.
        resp = client.get("/api/timecode_info")
        check("GET /api/timecode_info", resp.status_code == 200, resp.get_data(as_text=True))
        tc_info = resp.get_json()
        check("timecode_info reports clockType 3", tc_info.get("clockType") == 3, str(tc_info))
        check("timecode_info reports an NTSC label", "29.97" in tc_info.get("label", ""), str(tc_info))

        # -- timecode preview: converts a typed timecode to real seconds
        # without inserting anything, using D3's own Timecode class (see
        # config.py's module docstring for the live data this matches).
        resp = client.post(
            "/api/timecode_preview", data=json.dumps({"timecode": "1:45:00"}), content_type="application/json"
        )
        check("POST /api/timecode_preview", resp.status_code == 200, resp.get_data(as_text=True))
        preview = resp.get_json()
        check("preview normalizes '1:45:00'", preview.get("normalized") == "00:01:45:00", str(preview))
        check(
            "preview converts '1:45:00' @ 29.97fps to ~105.105s, not naive 105s",
            abs(preview.get("seconds", 0) - 105.105) < 0.001,
            str(preview),
        )
        resp = client.post(
            "/api/timecode_preview", data=json.dumps({"timecode": "not a timecode"}), content_type="application/json"
        )
        check("POST /api/timecode_preview rejects garbage with 400", resp.status_code == 400, resp.get_data(as_text=True))

        # -- timecode advance ("+15s" button): pure clock arithmetic, no
        # D3 round trip involved (see timecode_util.add_seconds's docstring).
        resp = client.post(
            "/api/timecode_add",
            data=json.dumps({"timecode": "1:45:00", "delta_seconds": 15}),
            content_type="application/json",
        )
        check("POST /api/timecode_add", resp.status_code == 200, resp.get_data(as_text=True))
        check("timecode_add '1:45:00' + 15s == '00:02:00:00'", resp.get_json().get("result") == "00:02:00:00", resp.get_data(as_text=True))
        resp = client.post(
            "/api/timecode_add",
            data=json.dumps({"timecode": "1.45.00", "delta_seconds": 15}),
            content_type="application/json",
        )
        check(
            "timecode_add accepts period-separated input",
            resp.get_json().get("result") == "00:02:00:00",
            resp.get_data(as_text=True),
        )

        # -- insert using the new timecode-based Start field (the actual
        # feature Josh asked for): "1:45:00" on a 29.97fps project must
        # place the layer/section at ~105.105 real seconds, NOT a naive
        # 105 -- this is a regression test for the exact bug report that
        # started this feature (105 real seconds displaying as "1:44:27",
        # not the "1:45:00" the user asked for).
        resp = client.post(
            "/api/insert",
            data=json.dumps(
                {
                    "track_name": "Preshow",
                    "start_timecode": "1:45:00",
                    "length_seconds": 4,
                    "create_section": True,
                    "asset_id": "042",
                    "mapping_numbers": ["1"],
                }
            ),
            content_type="application/json",
        )
        check("POST /api/insert with start_timecode", resp.status_code == 200, resp.get_data(as_text=True))
        tc_insert_result = resp.get_json()
        check("timecode insert ok=True", tc_insert_result.get("ok") is True, json.dumps(tc_insert_result))
        resp = client.get("/api/sections?track=Preshow")
        check("GET /api/sections on Preshow", resp.status_code == 200, resp.get_data(as_text=True))
        preshow_sections = resp.get_json()
        check(
            "section from start_timecode '1:45:00' landed at ~105.105s (not naive 105s)",
            any(abs(s.get("time", 0) - 105.105) < 0.001 for s in preshow_sections),
            str(preshow_sections),
        )

        # -- regression test for the reported bug: a 15-second insert
        # ending a frame before the next Section marker. The layer's END
        # (tEnd, which on the mock's identity timeToBeat equals real
        # seconds) must land EXACTLY where independently running
        # Timecode.fromString() on the ruler-advanced end timecode would
        # -- not wherever naive (start_seconds + 15.0) happens to round
        # to. Uses Josh's actual standard insert length (15s).
        expected_end_timecode = add_seconds("1:45:00", 15)
        check("expected end timecode is '2:00:00'", expected_end_timecode == "00:02:00:00", expected_end_timecode)
        expected_end_seconds = MockTimecode.fromString(expected_end_timecode, MockTimecode.SMPTE2997).t
        resp = client.post(
            "/api/insert",
            data=json.dumps(
                {
                    "track_name": "Preshow",
                    "start_timecode": "1:45:00",
                    "length_seconds": 15,
                    "asset_id": "042",
                    "mapping_numbers": ["1"],
                }
            ),
            content_type="application/json",
        )
        check("POST /api/insert with 15s standard length", resp.status_code == 200, resp.get_data(as_text=True))
        len15_result = resp.get_json()
        check(
            "response reports the resolved end_timecode",
            len15_result.get("end_timecode") == "00:02:00:00",
            json.dumps(len15_result),
        )
        len15_layer = len15_result["layers"][0]
        check(
            "15s insert's layer ends EXACTLY on the ruler-computed end (not naive start+15)",
            abs(len15_layer.get("tEnd", -1) - expected_end_seconds) < 1e-9,
            "tEnd={} expected={}".format(len15_layer.get("tEnd"), expected_end_seconds),
        )

        # -- timecode_preview with a length shows the same end point the
        # insert above just used, so the UI hint can be trusted BEFORE
        # clicking Insert.
        resp = client.post(
            "/api/timecode_preview",
            data=json.dumps({"timecode": "1:45:00", "length_seconds": 15}),
            content_type="application/json",
        )
        check("POST /api/timecode_preview with length_seconds", resp.status_code == 200, resp.get_data(as_text=True))
        preview_with_length = resp.get_json()
        check(
            "preview end_normalized matches the insert's end_timecode",
            preview_with_length.get("end_normalized") == "00:02:00:00",
            str(preview_with_length),
        )
        check(
            "preview end_seconds matches the insert's actual layer end",
            abs(preview_with_length.get("end_seconds", -1) - expected_end_seconds) < 1e-9,
            str(preview_with_length),
        )

        # -- discovery console must survive a landmine attribute (repr()/callable()
        # on some real D3 object types, e.g. 'Action', raises an uncatchable
        # TypeError -- see the 'inspect' template's comment in config.py, and the
        # real-world error this reproduces). `d3` is a generic Project resource
        # (projectName/majorVersion/etc, not a tracks/library/mappings namespace
        # -- confirmed live, see config.py's module docstring) that also carries
        # the guarded landmine attribute.
        resp = client.post("/api/discover", data=json.dumps({"target_expr": "d3"}), content_type="application/json")
        check("POST /api/discover on landmine-bearing object", resp.status_code == 200, resp.get_data(as_text=True))
        root_attrs = resp.get_json()["attributes"]
        check("discover found 'projectName' on d3", "projectName" in root_attrs, str(root_attrs))
        check("discover found 'majorVersion' on d3", "majorVersion" in root_attrs, str(root_attrs))
        check(
            "discover reported the guarded attribute's type without crashing",
            root_attrs.get("someGuardedThing") == "TYPE:FakeGuardedAction",
            str(root_attrs.get("someGuardedThing")),
        )

        # -- discovery console on resourceManager, the real way everything
        # else (tracks/media/mappings) is reached -- confirmed live.
        resp = client.post(
            "/api/discover", data=json.dumps({"target_expr": "resourceManager"}), content_type="application/json"
        )
        check("POST /api/discover on resourceManager", resp.status_code == 200, resp.get_data(as_text=True))
        rm_attrs = resp.get_json()["attributes"]
        check("resourceManager exposes allResources", "allResources" in rm_attrs, str(rm_attrs))
        check("resourceManager exposes load", "load" in rm_attrs, str(rm_attrs))

        # -- raw console
        resp = client.post("/api/console", data=json.dumps({"script": "return 1 + 1"}), content_type="application/json")
        check("POST /api/console", resp.status_code == 200, resp.get_data(as_text=True))
        check("console returnValue == 2", resp.get_json()["returnValue"] == 2, resp.get_data(as_text=True))

        # -- restoring default expression templates: this is the fix for the
        # exact real-world problem hit in production -- a saved config.json
        # holding a since-fixed built-in template (e.g. the old `tr.name`
        # bug) otherwise silently overrides the corrected code default
        # forever (see config.py's module docstring). Simulate a stale
        # save, then confirm the reset endpoint discards it.
        resp = client.post(
            "/api/config",
            data=json.dumps({"expressions": {"create_layer": "return {'ok': False, 'error': 'stale template'}"}}),
            content_type="application/json",
        )
        check("POST /api/config with a stale create_layer template", resp.status_code == 200)
        resp = client.get("/api/config")
        check(
            "stale template is in effect before reset",
            resp.get_json()["expressions"]["create_layer"] == "return {'ok': False, 'error': 'stale template'}",
            resp.get_json()["expressions"]["create_layer"],
        )
        resp = client.post("/api/config/reset_expressions")
        check("POST /api/config/reset_expressions", resp.status_code == 200, resp.get_data(as_text=True))
        restored = resp.get_json()["expressions"]["create_layer"]
        check("reset restores the real create_layer template", "tr.description" in restored, restored)
        check("reset discards the stale template", "stale template" not in restored, restored)

        resp = client.get("/api/version")
        check("GET /api/version", resp.status_code == 200, resp.get_data(as_text=True))
        version = resp.get_json()
        check(
            "version reports loaded_at_startup and current_on_disk for app.py",
            bool(version.get("loaded_at_startup", {}).get("app.py")) and bool(version.get("current_on_disk", {}).get("app.py")),
            version,
        )

    finally:
        mock_proc.terminate()
        try:
            mock_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock_proc.kill()
        os.unlink(tmp_cfg.name)

    print()
    if failures:
        print("{} check(s) FAILED: {}".format(len(failures), failures))
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
