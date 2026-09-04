"""
Persistent configuration for D3 Sequencer.

Everything the app does against your disguise/D3 server's Python object
model lives here as an editable "expression template" -- see the
"expressions" block below and README.md's "Object model" section. Nothing
here is executed against your server until you use it from the app.

Two sources fed these defaults, both now CONFIRMED against a live system
(a real show on Designer r32.4.11 build 253994), not guessed:

1. The live system's d3api.swagger.json (a real OpenAPI/Swagger spec for
   D3's REST API, basePath /api/session). It covers status/transport/etc.
   but has no endpoints for media/mapping listing or for creating
   layers/sections. Confirmed from it: there is no separate "Timeline"
   object above "Track" -- a Project directly contains Tracks
   (GET /transport/tracks), and Sections/Notes/Tags are per-Track
   "annotations" (GET /transport/annotations?name=<track>). d3_client.py
   uses these two endpoints directly over plain HTTP -- no scripting.
2. The official disguise Python API type stubs (d3.pyi, from
   https://developer.disguise.one/assets/d3.pyi) plus live, read-then-write
   testing against the real server via the Python Execution API
   (POST /python/execute). This confirmed the real object model used by the
   expressions below:
   - `d3` itself is the current Project *resource* -- not a namespace with
     `.tracks`/`.library`/`.mappings`. Everything is reached instead via the
     global `resourceManager.allResources(<Class>)`, which enumerates every
     live resource of a given type by its position in D3's resource tree.
   - Tracks: `resourceManager.allResources(Track)`. A Track has **no `.name`
     attribute at all** -- confirmed live the hard way (an uncatchable-
     looking `AttributeError: 'Track' object has no attribute 'name'`,
     same landmine class as the TestPatternModule one above). The display
     name shown everywhere in Designer and returned as "name" by the REST
     `/transport/tracks` endpoint is actually the Track resource's
     `.description` property -- confirmed live (`t.description == "track
     1"` matched the REST-reported name exactly). Match a track by name
     with `tr.description == track_name`, never `tr.name`.
   - Media ("Video"): a layer's module assigns a `VideoClip` resource
     (`resourceManager.allResources(VideoClip)`), not a raw file path. Each
     VideoClip's `.path` is a structured Path object; `.path.filename` is
     the actual on-disk filename your naming convention parses, and
     `str(.path)` is a stable string identifier you can hand back to
     `resourceManager.load(path_str, VideoClip)` later to fetch that exact
     resource again (confirmed round-trips correctly).
   - Mappings: what disguise calls "Mapping" in the layer inspector is a
     `Projection` resource (`resourceManager.allResources(Projection)`),
     NOT the `LedScreen` objects on the Stage (those are the underlying
     physical/virtual screens a Projection targets, reachable via
     `projection.screens`). `.description` is the human-readable mapping
     name shown in Designer (this is what matched your numbered mapping
     names, e.g. "01_vignette_a"); `str(.path)` is the stable identifier,
     same pattern as VideoClip.
   - A video layer's module is a `VariableVideoModule` (not `d3.VideoModule`
     -- that name doesn't exist). The Python API reference docs list
     `.video` (a VideoClip), `.mapping` (a Projection), `.mode` (int: {0:
     'Locked', 1: 'Normal'}), `.at_end_point` (int: {0: 'Loop', 1:
     'Ping-pong', 2: 'Pause'}) as plain, directly-settable properties on the
     module -- but THIS IS A TRAP, confirmed live the hard way over a whole
     debugging session with a real, silent production failure: assigning
     `m.video = resourceManager.load(...)` (etc.) is accepted with no error
     and even reads back correctly if you immediately re-read `m.video` --
     but it never reaches the layer's real, authoritative storage, and the
     video/mapping/mode/at-end-point stay at their untouched defaults
     forever as far as the timeline, the renderer, and Designer's own UI are
     concerned. The real storage is each field's `FieldSequence`
     (`layer.findSequence(fieldName)`), specifically its nested `.sequence`
     object -- confirmed against disguise's own sample "Hello World" text
     plugin, which sets its one field with
     `layer.findSequence('text').sequence.setString(0, "hello world")`.
     `video`/`mapping` are backed by a `ResourceSequence`: set with
     `.setResource(t, resource)`, read with `.evalResource(t)`.
     `mode`/`at_end_point` are backed by a `FloatSequence`: set with
     `.setFloat(t, value)`, read with `.key(t).v` (there is no `.evalFloat`
     -- confirmed live; `.key(t)` returns a `KeyFloat`/`KeyResource` object,
     and only `KeyFloat` has `.v`). `t=0` means "right at the start of the
     layer" -- fine for a static, non-animated value, which is all this app
     ever sets. **Always go through `findSequence(...).sequence`, never
     through the module's plain attributes, for these four fields.**
   - Layers are created with `track.addNewLayer(VariableVideoModule, tStart,
     tLength, name)`. `tStart`/`tLength` are in **track beats**, not
     seconds -- confirmed by the official Track & Sequencing guide. Convert
     from the seconds your UI works in with `track.timeToBeat(seconds)`
     (confirmed present on Track; handles any tempo map correctly, unlike
     a hardcoded seconds<->beats ratio).
   - Sections have **no name/label field** -- confirmed both by the REST
     schema (`transportSectionInfo`) and by d3.pyi's `SectionInfo` class
     (`iSection`, `tStart`, `tEnd`, `transition` only). They're created with
     `track.splitSectionAtBeat(beat)`, which splits/marks a Cue as a Section
     at that beat -- there's no `addSection(name, start)` call. This app
     creates the split but can't attach a name to it; see README.
   All of the above was validated with one live, reversible write test
   (create a throwaway layer with real Mapping/Video/Mode/At-end-point
   values, read every field back to confirm it stuck, then
   `track.removeLayer(...)` it) before being written into the defaults
   below -- see README's "Object model" section for the full trail.

Timecode / NTSC frame rates (the "start field in minutes:seconds" feature):
   `track.timeToBeat(seconds)` (used throughout create_layer/create_section
   above) is plain, frame-rate-agnostic real-world seconds, driven only by
   the track's bpm -- confirmed live (Josh's track: bpm=60.0, so 1 beat ==
   1 real second exactly on that track). It has NOTHING to do with the
   NTSC/drop-frame mismatch described below; that mismatch is entirely
   about how the timeline ruler DISPLAYS elapsed real time as a timecode,
   not about beats/tempo.
   - At NTSC broadcast rates (29.97, 59.94, 23.976fps), Designer's timeline
     ruler shows a timecode that runs slightly slower than real time (the
     well-known NTSC ~0.1% drift): 105 real seconds after a layer's start
     displays as "1:44:27", not "1:45:00" -- confirmed live and exactly
     reproduced (see below), and it's what Josh independently reported
     seeing before this feature existed.
   - The project's active clock type is
     `guisystem.currentTransportManager.smpteClockType()` -- an int,
     confirmed live to be one of exactly 6 values, each a `Timecode` class
     constant: `Timecode.SMPTE23976`=0, `SMPTE24`=1, `SMPTE25`=2,
     `SMPTE2997`=3, `SMPTE2997DF`=4, `SMPTE30`=5 (confirmed against the
     live Timecode class's own `Variables`, not guessed). This is NOT an
     attribute of Track -- Track's full `dir()` has no fps/frame/rate/
     smpte/drop-named attribute at all, confirmed live. Josh's project
     reports clockType 3 (SMPTE2997 -- 29.97fps, NON-drop), matching what
     he described ("My current timeline is in 29.97").
   - D3 has its own native `Timecode` class that does the real/displayed
     time conversion correctly for whatever clock type is active --
     confirmed live to exactly match Designer's own behavior, so this app
     delegates to it entirely rather than hand-rolling NTSC/drop-frame
     math (see timecode_util.py's docstring for why that matters,
     especially for drop-frame's frame-number-skipping rules):
       `Timecode(t_seconds, clockType)` -- construct from real seconds.
       `.asString(highRes)` -- render as display text, e.g. "00:01:44.27"
         (colons between H/M/S, a PERIOD before the frames field -- this
         exact format, confirmed live).
       `.fps()` -- the clock type's actual playback fps (e.g. 29.97).
       `.t` -- read the real-seconds value back off an instance.
       `Timecode.fromString(text, clockType)` -- parse a timecode STRING
         (accepts ':' or '.' before the frames field, confirmed live) back
         to real seconds via `.t`.
     Live validation, exact match, against Josh's project (track "track 1",
     clockType 3):
       `Timecode(105.0, 3).asString(False)` == `"00:01:44.27"` -- this is
         the precise value Josh reported seeing for a layer placed at 105
         real seconds, confirmed to the exact frame.
       `Timecode.fromString("00:01:45:00", 3).t` == `105.10510751317817`
         -- i.e. what a user typing "1:45:00" (what they see on the ruler)
         actually means in real seconds; feeding this into
         `track.timeToBeat()` places the layer exactly where the ruler
         number says.
   - Only clock types reachable on Josh's system so far (2 == SMPTE25 by
     inference from the enum, 3 == SMPTE2997 confirmed live) have been
     validated against a live project. SMPTE2997DF (drop-frame, 4) is
     supported by the same `timecode_to_seconds`/`timecode_info`
     expressions (D3's own Timecode class handles it, not this app's
     code), but has NOT been live-validated against an actual drop-frame
     D3 project -- if Josh ever switches a project to drop-frame, it's
     worth re-confirming the round trip the same way as above before
     trusting it. Frame rates above 30 (50/59.94/60, which Josh mentioned
     he also uses) do NOT appear as separate `Timecode` clock-type
     constants -- only 0..5 above exist -- so how D3 represents a
     59.94/60fps project's timecode via this same API is still unconfirmed
     and should be checked live against such a project before relying on
     it; `timecode_info`'s reported `fps` value is the quickest way to
     spot-check that live.
   - See timecode_util.py for the (purely cosmetic) user-input parsing
     that sits in front of `Timecode.fromString()`, and d3_client.py's
     `get_timecode_info()` / `timecode_to_seconds()` for how the two new
     expressions above are called.
   - A layer's END must NOT be computed as `track.timeToBeat(start_seconds
     + length_seconds)` when start_seconds came from a ruler timecode --
     this was a real, reported bug (a 15-second insert landing a frame
     before the next Section marker). Confirmed live, directly against
     Josh's project, for exactly his standard case (start "1:45:00",
     length 15s, clockType 3): naively adding 15.0 real seconds to
     start_seconds (105.10510751317817) and converting once gives
     120.10510751317817s, but independently parsing the ruler-advanced
     end timecode "2:00:00" gives 120.12012287220362s instead -- a
     0.015s (~0.45 frame) gap. That's large enough, combined with
     whatever exact rounding Designer applies internally to decide which
     frame a real-time value's boundary falls on, to visibly land the
     naive version a frame short of where the ruler says "2:00:00"
     actually is -- matching the report exactly. The fix (see app.py's
     `/api/insert`): treat Length as whole seconds of RULER time, not
     real time -- advance the Start timecode string by `length_seconds`
     via `timecode_util.add_seconds()` (the same pure clock arithmetic
     the "+15s" button uses) to get an end timecode, then run THAT
     through `Timecode.fromString()` independently, exactly like Start.
     Both ends are then each individually frame-exact on the ruler, with
     no real-seconds arithmetic step in between to round unpredictably.
     `length_seconds` passed to `create_layer` ends up as
     `end_seconds - start_seconds` (both already ruler-exact), so the
     `create_layer`/`create_section` expressions below needed NO changes
     for this fix -- it's entirely in how app.py computes the seconds it
     hands them.

Two more things worth knowing about this Python Execution API's error
behaviour (also baked into the `inspect` template below):
- Some disguise object-model types (observed: `Action`) raise a TypeError
  the instant *anything* touches them -- even `type()` -- and this is NOT
  reliably catchable with `try/except` from inside a script (it crosses a
  native/plugin boundary uncaught). The only reliable mitigation is to
  never touch an attribute whose *name* looks like it might be one of
  these (e.g. skip anything containing "action") before ever calling
  `getattr` on it.
- Calling `getattr(obj, name)` for a `name` that is valid for *some*
  objects of that general kind but not for this specific instance's actual
  class (e.g. assuming every layer's `.module` is a `VariableVideoModule`
  when some are actually `TestPatternModule` or `GroupLayer`) raises an
  `AttributeError` that is *also* not reliably catchable by try/except.
  The reliable fix is the same shape: check `name in dir(obj)` (or check
  the object's `type(obj).__name__`) before calling `getattr`, rather than
  calling it and hoping to catch a failure.
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy

CONFIG_PATH = os.environ.get("D3SEQ_CONFIG_PATH", os.path.join(os.path.dirname(__file__), "config.json"))

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "server": {
        "host": "10.10.22.101",
        "port": 80,
        "scheme": "http",
        "timeout_seconds": 15,
    },
    # mapping_no (string, as found in filenames) -> the resource_path
    # (str(projection.path), a stable identifier -- see module docstring)
    # of the actual disguise Projection/mapping you want that number to
    # mean. Populate this on the Setup page once you've fetched mappings
    # from your server.
    "mapping_map": {},
    # Free-form notes captured during setup, purely informational.
    "notes": "",
    # Python-2.7 expression/script templates sent to the D3 Execution API.
    # `{placeholder}` fields are filled in with str.format() as
    # already-quoted Python literals -- see d3_client.py for exactly which
    # placeholders are available for each key. These are CONFIRMED against
    # a live system (see module docstring) -- if a future Designer version
    # changes the object model, use the Discovery Console to find what
    # changed and edit these from the Setup page without touching code.
    "expressions": {
        # Should return a JSON-serializable list of dicts, one per media
        # clip in the library, each with "filename" (for the naming
        # convention parser) and "resource_path" (a stable identifier to
        # hand back to create_layer).
        "media_list": (
            "items = []\n"
            "for c in resourceManager.allResources(VideoClip):\n"
            "    try:\n"
            "        p = c.path\n"
            "        items.append({'filename': p.filename, 'resource_path': str(p)})\n"
            "    except Exception as e:\n"
            "        pass\n"
            "return items"
        ),
        # Should return a JSON-serializable list of {"name", "resource_path"}
        # for every Projection ("Mapping" in the Designer UI) available in
        # the show, to populate the Setup page.
        "mapping_list": (
            "items = []\n"
            "for p in resourceManager.allResources(Projection):\n"
            "    try:\n"
            "        items.append({'name': p.description, 'resource_path': str(p.path)})\n"
            "    except Exception as e:\n"
            "        pass\n"
            "return items"
        ),
        # Creates one VariableVideoModule layer on the Track named
        # {track_name}, at {start_seconds} for {length_seconds} (converted
        # to beats via track.timeToBeat() -- see module docstring), named
        # {layer_name}, loading the VideoClip at {resource_path} mapped to
        # the Projection at {mapping_resource_path}, with the given
        # {mode} ("Normal"/"Locked") and {end_mode} ("loop"/"ping-pong"/
        # "pause").
        # SECOND landmine, confirmed live the hard way AFTER fixing the
        # first one below: `addNewLayer` implicitly creates each of these
        # four FieldSequences with ONE default key (holding the untouched
        # default value) already sitting at the layer's own start beat.
        # Calling `.setResource(0, ...)` / `.setFloat(0, ...)` with a
        # hardcoded `0` -- as disguise's own sample "Hello World" plugin
        # does, and as an earlier version of this template did -- adds a
        # SECOND, conflicting key at absolute beat 0 instead of replacing
        # that default key, UNLESS the layer happens to start at beat 0.
        # For any layer starting elsewhere (i.e. every real insert), the
        # renderer/Designer's UI evaluate the sequence at the layer's own
        # start beat and later, where the ORIGINAL default key (None video,
        # default mapping, defaults for mode/at-end-point) is still in
        # effect and wins -- so the layer silently keeps its defaults even
        # though a keyframe with the right value now also exists, just at
        # the wrong (irrelevant, before-the-layer-starts) time. The fix:
        # always pass `start_beat` (the same beat the layer itself starts
        # at), never a hardcoded `0`, as the time argument -- this REPLACES
        # the existing default key at that exact beat instead of adding a
        # second one.
        #
        # IMPORTANT, confirmed live the hard way: `layer.module.video = ...`
        # (a plain attribute assignment) is NOT the real, authoritative way
        # to set these fields, despite `video`/`mapping`/`mode`/
        # `at_end_point` all being documented as plain properties on
        # VariableVideoModule/Module. In practice, assigning them directly
        # is silently accepted and even reads back correctly if you re-read
        # `layer.module.video` right away -- but it never reaches the
        # layer's real FieldSequence/KeySequence data, which is what the
        # timeline, the renderer, and Designer's own UI actually evaluate.
        # The real pattern (confirmed against disguise's own sample "Hello
        # World" text plugin, which does
        # `layer.findSequence('text').sequence.setString(0, "hello world")`)
        # is: `layer.findSequence(fieldName).sequence` is a ResourceSequence
        # (for video/mapping -- set with `.setResource(t, resource)`, read
        # with `.evalResource(t)`) or a FloatSequence (for mode/
        # at_end_point -- set with `.setFloat(t, value)`, read with
        # `.key(t).v`). `t=0` means "at the very start of the layer", which
        # is all a static (non-animated) value needs -- one keyframe.
        "create_layer": (
            "track = None\n"
            "for tr in resourceManager.allResources(Track):\n"
            "    if tr.description == {track_name}:\n"
            "        track = tr\n"
            "        break\n"
            "if track is None:\n"
            "    return {{'ok': False, 'error': 'track not found'}}\n"
            "start_beat = track.timeToBeat({start_seconds})\n"
            "end_beat = track.timeToBeat({start_seconds} + {length_seconds})\n"
            "length_beat = end_beat - start_beat\n"
            "layer = track.addNewLayer(VariableVideoModule, start_beat, length_beat, {layer_name})\n"
            "layer.findSequence('video').sequence.setResource(start_beat, resourceManager.load({resource_path}, VideoClip))\n"
            "layer.findSequence('mapping').sequence.setResource(start_beat, resourceManager.load({mapping_resource_path}, Projection))\n"
            "layer.findSequence('mode').sequence.setFloat(start_beat, {{'Locked': 0, 'Normal': 1}}[{mode}])\n"
            "layer.findSequence('at_end_point').sequence.setFloat(start_beat, {{'loop': 0, 'ping-pong': 1, 'pause': 2}}[{end_mode}])\n"
            "return {{'ok': True, 'layer_name': layer.name, 'tStart': layer.tStart, 'tEnd': layer.tEnd}}"
        ),
        # Diagnostic helper: run immediately after create_layer (a separate
        # script execution, on purpose -- it re-fetches the layer fresh from
        # D3 rather than trusting anything held over from the create call)
        # to confirm what the layer's fields actually ended up holding.
        # Reads via the same FieldSequence/KeySequence path create_layer
        # writes through (see its comment above) -- NOT via
        # `layer.module.video` etc, which do not reliably reflect the real
        # value. Added because of a real bug where create_layer reported
        # ok=True with the right layer name/position, but the video/mapping
        # fields silently stayed at their untouched defaults -- this makes
        # that visible in the /api/insert response instead of only being
        # visible by clicking into the layer in Designer afterward.
        # Evaluates at `layer.tStart` (the layer's OWN actual start beat),
        # never a hardcoded 0 -- reading at 0 is a tautology that hid the
        # real bug from this very diagnostic for a full day: it will always
        # match whatever create_layer just wrote at time 0, regardless of
        # whether that time is actually within the layer's active range
        # (see create_layer's comment above). Reading at the layer's own
        # tStart instead reflects what the renderer/Designer's UI actually
        # see when the layer plays.
        "verify_layer": (
            "for tr in resourceManager.allResources(Track):\n"
            "    if tr.description == {track_name}:\n"
            "        for layer in tr.layers:\n"
            "            if layer.name == {layer_name}:\n"
            "                video_val = layer.findSequence('video').sequence.evalResource(layer.tStart)\n"
            "                mapping_val = layer.findSequence('mapping').sequence.evalResource(layer.tStart)\n"
            "                mode_val = layer.findSequence('mode').sequence.key(0).v\n"
            "                aep_val = layer.findSequence('at_end_point').sequence.key(0).v\n"
            "                return {{\n"
            "                    'found': True,\n"
            "                    'video': str(video_val) if video_val is not None else None,\n"
            "                    'mapping': str(mapping_val) if mapping_val is not None else None,\n"
            "                    'mode': mode_val,\n"
            "                    'at_end_point': aep_val,\n"
            "                }}\n"
            "        return {{'found': False, 'reason': 'layer not found on track'}}\n"
            "return {{'found': False, 'reason': 'track not found'}}"
        ),
        # Splits the Track named {track_name} into a new Section at
        # {start_seconds} (converted to beats via track.timeToBeat()).
        # Sections have no name/label field on this system -- see module
        # docstring -- so there is no name placeholder here.
        "create_section": (
            "track = None\n"
            "for tr in resourceManager.allResources(Track):\n"
            "    if tr.description == {track_name}:\n"
            "        track = tr\n"
            "        break\n"
            "if track is None:\n"
            "    return {{'ok': False, 'error': 'track not found'}}\n"
            "start_beat = track.timeToBeat({start_seconds})\n"
            "track.splitSectionAtBeat(start_beat)\n"
            "return {{'ok': True, 'beat': start_beat}}"
        ),
        # Reports the project's current SMPTE timecode display setting, so
        # the UI can show e.g. "29.97 fps (NTSC, Non-Drop)" next to the
        # Start field. `guisystem.currentTransportManager.smpteClockType()`
        # is confirmed live as the correct source (it returns "the transport
        # manager's SMPTE clock type, falling back to the project clock type"
        # per the official docs) -- NOT anything on Track itself (Track has
        # no fps/frame/rate/smpte-named attribute at all, confirmed by
        # enumerating its full dir() live). See the module docstring's
        # "Timecode / NTSC frame rates" section for the confirmed clockType
        # values and how this is used.
        "timecode_info": (
            "ct = guisystem.currentTransportManager.smpteClockType()\n"
            "return {{'clockType': ct, 'fps': Timecode(0.0, ct).fps()}}"
        ),
        # Converts a normalized 'HH:MM:SS:FF' timecode string (see
        # timecode_util.normalize_timecode -- that function only handles
        # cosmetic format flexibility, never the actual time math) to real
        # elapsed seconds, using D3's OWN Timecode.fromString() and the
        # project's live SMPTE clock type. This is what makes NTSC/
        # drop-frame timelines place layers where the displayed ruler number
        # actually says, instead of where naive decimal seconds would put
        # them -- see the module docstring for the confirmed live data point
        # this was validated against. {timecode} arrives already quoted
        # (a Python string literal) via py_literal(), same as every other
        # placeholder here.
        "timecode_to_seconds": (
            "ct = guisystem.currentTransportManager.smpteClockType()\n"
            "tc = Timecode.fromString({timecode}, ct)\n"
            "return {{'seconds': tc.t, 'clockType': ct}}"
        ),
        # Generic object introspection helper used by the Discovery Console.
        # {target_expr} is any Python expression the user types in, e.g.
        # "d3" or "resourceManager.allResources(Track)[0]" -- unlike the
        # other placeholders this one is inserted VERBATIM (it's code, not
        # a value), so it does not get repr()'d by d3_client.py.
        #
        # Safety, confirmed live (see module docstring for why both
        # matter):
        # 1. Never call getattr() for an attribute name containing
        #    "action" -- some disguise types (observed: 'Action') raise an
        #    uncatchable TypeError the instant anything touches them.
        # 2. Only ever call type()/isinstance() on the retrieved value,
        #    never repr()/callable() -- same landmine.
        # Because `name` always comes from dir({target_expr}) on this same
        # object, the "attribute valid for some-but-not-this instance"
        # AttributeError landmine (seen when assuming every Layer's
        # .module is a VariableVideoModule) doesn't apply here -- that one
        # only bites when you hand-write attribute names for a DIFFERENT
        # object than the one you're calling dir() on.
        "inspect": (
            "out = {{}}\n"
            "for name in dir({target_expr}):\n"
            "    if name.startswith('_'):\n"
            "        continue\n"
            "    if 'action' in name.lower():\n"
            "        out[name] = 'SKIPPED (guarded type landmine -- see config.py)'\n"
            "        continue\n"
            "    try:\n"
            "        val = getattr({target_expr}, name)\n"
            "        tname = type(val).__name__\n"
            "        if isinstance(val, (str, unicode, int, long, float, bool, type(None))):\n"
            "            out[name] = '%s = %r' % (tname, val)\n"
            "        elif isinstance(val, (list, tuple)):\n"
            "            out[name] = '%s[%d]' % (tname, len(val))\n"
            "        elif isinstance(val, dict):\n"
            "            out[name] = 'dict[%d]' % len(val)\n"
            "        else:\n"
            "            out[name] = 'TYPE:%s' % tname\n"
            "    except Exception as e:\n"
            "        out[name] = 'ERROR: %s' % e\n"
            "return out"
        ),
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            cfg = deepcopy(DEFAULT_CONFIG)
            _write(cfg)
            return cfg
        with open(CONFIG_PATH, "r") as f:
            try:
                on_disk = json.load(f)
            except json.JSONDecodeError:
                on_disk = {}
        # Merge onto defaults so new keys added in future versions of this
        # app show up without clobbering the user's saved values.
        return _deep_merge(DEFAULT_CONFIG, on_disk)


def save_config(cfg: dict) -> dict:
    with _lock:
        merged = _deep_merge(DEFAULT_CONFIG, cfg)
        _write(merged)
        return merged


def _write(cfg: dict) -> None:
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    os.replace(tmp_path, CONFIG_PATH)


def default_expressions() -> dict:
    """A fresh copy of the built-in expression templates, straight from
    DEFAULT_CONFIG -- for the Setup page's "Restore Default Expression
    Templates" button. Saved expressions otherwise stick around forever
    (see load_config()'s docstring: on-disk values win over new code
    defaults for keys that already exist), which is the right behaviour
    for your own edits but means a bug fix to a *built-in* template
    doesn't reach an existing config.json on its own -- this is the
    one-click way out of that instead of deleting config.json by hand."""
    return deepcopy(DEFAULT_CONFIG["expressions"])


def update_config(patch: dict) -> dict:
    """Apply a partial update from the API. Each top-level key in `patch`
    (e.g. 'server', 'mapping_map', 'expressions') REPLACES the
    corresponding saved value wholesale, rather than being deep-merged into
    it. The frontend always sends these as complete objects, and a
    wholesale replace is what lets removing something -- e.g. a mapping
    number row -- actually take effect instead of the old value being
    silently merged back in. (This is deliberately different from
    load_config()'s deep merge, which exists only to backfill NEW keys a
    future version of this app adds to DEFAULT_CONFIG into an older
    on-disk config.json -- that one must stay a deep merge.)"""
    cfg = load_config()
    for k, v in patch.items():
        cfg[k] = v
    return save_config(cfg)
