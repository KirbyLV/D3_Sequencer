"""
A minimal stand-in for a disguise/D3 Designer instance's two HTTP APIs.

Unlike the previous version of this file, the Python object model below is
NOT a guess -- it mirrors what live testing against a real system (Designer
r32.4.11, via the official d3.pyi type stubs plus a live read/write test)
confirmed. See config.py's module docstring for the full trail. In
particular:

- `d3` is a generic Project *resource* object (rename/duplicate/etc.), not
  a namespace with `.tracks`/`.library`/`.mappings`.
- Everything else the app needs is reached via top-level globals bound into
  the script's namespace, exactly like the real Execution API: `Track`,
  `VideoClip`, `Projection`, `VariableVideoModule` (class objects, used both
  as type tokens for `resourceManager.allResources(...)` and as the actual
  classes of the fake resources below), and `resourceManager` itself
  (`allResources(cls)`, `load(path_str, cls)`).
- A Track's layers are created with `addNewLayer(moduleType, startBeat,
  lengthBeat, name)`; `timeToBeat(seconds)` converts to beats (the mock uses
  a 1-beat-per-second tempo, same as the live system's default track);
  `splitSectionAtBeat(beat)` creates an unnamed Section; `removeLayer(layer)`
  removes one.
- A layer's `.module` (a `VariableVideoModule`) has `.video` (a VideoClip),
  `.mapping` (a Projection), `.mode` (int, 0=Locked/1=Normal), and
  `.at_end_point` (int, 0=Loop/1=Ping-pong/2=Pause) -- these exact names and
  option codes come from the official d3.pyi docstrings.
- Resources (VideoClip, Projection, Track) have a `.path` whose `str()` is a
  stable identifier `resourceManager.load(path_str, cls)` can round-trip.

It implements two things confirmed by the live system's d3api.swagger.json
(Swagger 2.0, basePath /api/session):

1. GET /transport/tracks and GET /transport/annotations -- real, confirmed
   REST endpoints. This mock's shapes for these come straight from the
   swagger schemas (transportListTracksResponse, transportTrackInfo,
   transportListAnnotationsResponse, transportSectionInfo), so d3_client.py's
   REST helpers can be exercised against something schema-accurate.
2. POST /python/execute -- confirmed request/response *shape*
   (pythonExecuteRequest/pythonExecuteResponse: {"script"} in,
   {"status", "d3Log", "pythonLog", "returnValue"} out, returnValue always a
   JSON string on the wire).
"""
from __future__ import annotations

import io
import itertools
import json
import sys
import textwrap
from contextlib import redirect_stdout

from flask import Flask, jsonify, request

app = Flask(__name__)


# ---- fake d3 Python object model (confirmed shape, see module docstring) --


class FakePath(object):
    """Mimics disguise's structured Path object: str()'s to the stable
    resource identifier, and exposes .filename for the naming convention
    parser."""

    def __init__(self, path_str, filename=None):
        self._path_str = path_str
        self.filename = filename

    def __str__(self):
        return self._path_str


class VariableVideoModule(object):
    """A video layer's module. Attribute names/option codes below are
    confirmed from the official d3.pyi type stubs AND the live Python API
    reference docs -- but confirmed live the hard way (a real insert that
    silently did nothing) that `.video`/`.mapping`/`.mode`/`.at_end_point`
    are NOT the real, authoritative storage for these fields: they read
    back whatever you last wrote to them, but a plain assignment through
    them never reaches the layer's actual FieldSequence/KeySequence data,
    which is what the timeline/renderer/Designer's own UI actually
    evaluate. This mock deliberately does NOT wire these attributes up to
    the sequences below, so a regression back to `m.video = ...` in
    create_layer fails the smoke test's sequence-based verify_layer check
    immediately, instead of only failing silently against a real server."""

    def __init__(self):
        self.video = None  # VideoClip -- NOT the real storage, see above
        self.mapping = None  # Projection -- NOT the real storage, see above
        self.mode = 1  # 0: Locked, 1: Normal -- NOT the real storage, see above
        self.at_end_point = 0  # 0: Loop, 1: Ping-pong, 2: Pause -- NOT the real storage, see above


class FakeKey(object):
    """Mimics KeyFloat/KeyResource. Confirmed live: a resource key's value
    is read via `.r`, a numeric key's via `.v` -- NOT a shared `.v`/`.value`
    -- accessing the wrong one for a key's actual flavor raises the
    uncatchable-AttributeError landmine (see config.py's module docstring).
    This mock sets whichever of `.r`/`.v` is relevant and leaves the other
    genuinely absent, so a test that reads the wrong one fails loudly."""

    def __init__(self, localT, resource=None, is_float=False, value=None):
        self.localT = localT
        if is_float:
            self.v = value
        else:
            self.r = resource


class FakeSequence(object):
    """Mimics ResourceSequence/FloatSequence -- the REAL, authoritative
    per-field storage confirmed live (see the `layer.findSequence(...)`
    pattern used by disguise's own sample "Hello World" text plugin:
    `layer.findSequence('text').sequence.setString(0, "hello world")`).

    CRITICAL, confirmed live the hard way: `addNewLayer` implicitly creates
    each field's sequence with ONE default key already sitting at the
    layer's own start beat (`tStart`) -- not at beat 0. `setResource`/
    `setFloat` REPLACE the key at the given time if one already exists
    there, or ADD a new one otherwise -- they do not touch/remove other
    keys. A caller that writes to time 0 while the layer starts elsewhere
    therefore ends up with TWO keys: the original default (still in effect
    at and after `tStart`, which is where the renderer/Designer's UI
    actually evaluate it) and an inert one at 0. This is the real bug this
    app hit -- create_layer must write at the layer's own `start_beat`,
    never a hardcoded 0 -- so this mock reproduces the multi-key/held-value
    behavior instead of collapsing to a single always-current value, to
    catch a regression back to time-0 writes.

    Real ResourceSequence only has setResource/evalResource and real
    FloatSequence only has setFloat/key(t).v -- this mock is intentionally
    permissive and supports both, since the app only ever calls the flavor
    that matches the field it's addressing."""

    def __init__(self, start_beat, initial=None, is_float=False):
        self._is_float = is_float
        self._keys = [FakeKey(start_beat, resource=initial, is_float=is_float, value=initial)]

    def _set(self, t, value):
        for k in self._keys:
            if k.localT == t:
                if self._is_float:
                    k.v = value
                else:
                    k.r = value
                return
        self._keys.append(FakeKey(t, resource=None if self._is_float else value, is_float=self._is_float, value=value if self._is_float else None))
        self._keys.sort(key=lambda k: k.localT)

    def setResource(self, t, value):
        self._set(t, value)

    def evalResource(self, t):
        # Held/step semantics: the latest key at or before t wins (matching
        # confirmed live behavior); before the first key, the first key's
        # value is used.
        applicable = [k for k in self._keys if k.localT <= t]
        winner = max(applicable, key=lambda k: k.localT) if applicable else self._keys[0]
        return winner.r

    def setFloat(self, t, value):
        self._set(t, value)

    def key(self, i):
        return self._keys[i]

    def nKeys(self):
        return len(self._keys)


class FakeFieldSequence(object):
    """Mimics FieldSequence -- what `layer.findSequence(name)` returns.
    The actual settable/readable object is one level deeper, at `.sequence`
    -- confirmed live."""

    def __init__(self, sequence):
        self.sequence = sequence


class Layer(object):
    def __init__(self, name, tStart, tLength):
        self.name = name
        self.tStart = tStart
        self.tLength = tLength
        self.tEnd = tStart + tLength
        self.module = VariableVideoModule()
        self._sequences = {
            "video": FakeSequence(tStart, None),
            "mapping": FakeSequence(tStart, None),
            "mode": FakeSequence(tStart, 1.0, is_float=True),
            "at_end_point": FakeSequence(tStart, 0.0, is_float=True),
        }

    def findSequence(self, name):
        return FakeFieldSequence(self._sequences[name])


_section_index = itertools.count(1)


class Track(object):
    """No `.name` attribute on the real Track class -- confirmed live the
    hard way (an uncatchable AttributeError). The display name shown in
    Designer and returned as "name" by the REST /transport/tracks endpoint
    is really the resource's `.description` -- confirmed live. This mock
    deliberately has no `.name` either, so a regression back to `tr.name`
    in the create_layer/create_section templates fails the smoke test
    immediately instead of only failing against a real server."""

    def __init__(self, description, length_seconds):
        self.description = description
        self.uid = str(abs(hash(description)))
        self.length = length_seconds
        self.layers = []
        self.sections = []  # REST-shaped dicts, see transportSectionInfo
        self.path = FakePath("objects/track/{}.apx".format(description))

    def timeToBeat(self, t_sec):
        # The real system's default (no tempo map) track behaved as
        # 1 beat == 1 second in live testing; the mock mirrors that.
        return t_sec

    def addNewLayer(self, module_type, start_beat, length_beat, name):
        layer = Layer(name, start_beat, length_beat)
        self.layers.append(layer)
        return layer

    def removeLayer(self, layer):
        if layer in self.layers:
            self.layers.remove(layer)
            return 0
        return -1

    def splitSectionAtBeat(self, beat):
        section = {
            "time": beat,
            "index": str(next(_section_index)),
            "crossfadeMode": "Off",
            "crossfadeDuration": 0,
            "loopCrossfade": False,
        }
        self.sections.append(section)
        return section


class VideoClip(object):
    def __init__(self, filename, directory="objects/videoclip/test_content/"):
        self.path = FakePath(directory + filename + ".apx", filename)


class Projection(object):
    def __init__(self, description, category="dynamicfeedprojection"):
        self.description = description
        self.path = FakePath("objects/{}/{}.apx".format(category, description))
        self.screens = []


def _parse_timecode_string(s):
    """Accepts 'HH:MM:SS:FF' or 'HH:MM:SS.FF' -- confirmed live that D3's
    real Timecode.fromString() tolerates both separators before the frames
    field (see config.py's module docstring)."""
    if "." in s:
        main, ff = s.rsplit(".", 1)
    else:
        parts = s.split(":")
        if len(parts) != 4:
            raise ValueError("bad timecode string: {!r}".format(s))
        main, ff = ":".join(parts[:3]), parts[3]
    hh, mm, ss = (int(p) for p in main.split(":"))
    return hh, mm, ss, int(ff)


def _dropframe_hmsf_to_frames(hh, mm, ss, ff, nominal_fps, drop_per_min):
    """Standard SMPTE drop-frame timecode -> frame-count algorithm (the
    well-known Andrew-Duncan-style formula). Implements the textbook
    math; NOT verified against a live drop-frame D3 project -- see
    config.py's module docstring."""
    total_minutes = 60 * hh + mm
    frame_number = (nominal_fps * 3600 * hh) + (nominal_fps * 60 * mm) + (nominal_fps * ss) + ff
    frame_number -= drop_per_min * (total_minutes - total_minutes // 10)
    return frame_number


def _dropframe_frames_to_hmsf(frame_number, nominal_fps, drop_per_min):
    """Inverse of _dropframe_hmsf_to_frames -- standard textbook
    drop-frame algorithm, NOT verified against a live drop-frame D3
    project -- see config.py's module docstring."""
    frames_per_minute = nominal_fps * 60 - drop_per_min
    frames_per_10_minutes = nominal_fps * 60 * 10
    d, m = divmod(frame_number, frames_per_10_minutes)
    if m < drop_per_min:
        adjusted = frame_number - (drop_per_min * d)
    else:
        adjusted = frame_number - (drop_per_min * d) - (drop_per_min * ((m - drop_per_min) // frames_per_minute))
    frames = adjusted % nominal_fps
    total_secs = adjusted // nominal_fps
    seconds = total_secs % 60
    minutes = (total_secs // 60) % 60
    hours = total_secs // 3600
    return hours, minutes, seconds, frames


class Timecode(object):
    """Mimics disguise's real Timecode class -- confirmed live (see
    config.py's module docstring, "Timecode / NTSC frame rates") to
    convert between real elapsed seconds and the displayed SMPTE
    timecode for whatever clock type is active, including the NTSC
    ~0.1% real-time drift. The 6 clockType constants and their meaning
    (SMPTE23976=0 .. SMPTE30=5), `.asString()`'s "HH:MM:SS.FF" format
    (period before frames), `.t`, `.fps()`, and `.fromString()` accepting
    either ':' or '.' before the frames field are all confirmed live --
    see tests/run_smoke_test.py for the exact live-captured data point
    (105.0s @ clockType 3 == "00:01:44.27") this is checked against.

    Drop-frame (SMPTE2997DF, clockType 4) uses the standard textbook
    drop-frame algorithm -- NOT live-verified against an actual
    drop-frame D3 project, since none was available to test against."""

    SMPTE23976 = 0
    SMPTE24 = 1
    SMPTE25 = 2
    SMPTE2997 = 3
    SMPTE2997DF = 4
    SMPTE30 = 5

    _NOMINAL_FPS = {0: 24, 1: 24, 2: 25, 3: 30, 4: 30, 5: 30}
    _NTSC_SCALED = frozenset({0, 3, 4})  # actual fps = nominal * 1000/1001
    _DROP_FRAME = frozenset({4})
    _DROP_PER_MIN = 2

    def __init__(self, t_=0.0, clockType_=SMPTE30, customFps=None):
        self.t = float(t_)
        self.clockType = clockType_
        self._customFps = customFps

    def fps(self):
        nominal = self._NOMINAL_FPS[self.clockType]
        if self.clockType in self._NTSC_SCALED:
            return nominal * 1000.0 / 1001.0
        return float(nominal)

    def asString(self, highResolution=False):
        nominal = self._NOMINAL_FPS[self.clockType]
        total_frames = int(round(self.t * self.fps()))
        if self.clockType in self._DROP_FRAME:
            hh, mm, ss, ff = _dropframe_frames_to_hmsf(total_frames, nominal, self._DROP_PER_MIN)
        else:
            ff = total_frames % nominal
            total_secs = total_frames // nominal
            ss = total_secs % 60
            mm = (total_secs // 60) % 60
            hh = total_secs // 3600
        base = "{:02d}:{:02d}:{:02d}.{:02d}".format(hh, mm, ss, ff)
        return base + ".00" if highResolution else base

    @staticmethod
    def fromString(s, clockType):
        hh, mm, ss, ff = _parse_timecode_string(s)
        nominal = Timecode._NOMINAL_FPS[clockType]
        actual_fps = Timecode(0.0, clockType).fps()
        if clockType in Timecode._DROP_FRAME:
            total_frames = _dropframe_hmsf_to_frames(hh, mm, ss, ff, nominal, Timecode._DROP_PER_MIN)
        else:
            total_frames = ((hh * 3600 + mm * 60 + ss) * nominal) + ff
        return Timecode(total_frames / actual_fps, clockType)

    @staticmethod
    def clockTypeToUiString(clockType, customFps=None):
        return {
            0: "23.976 fps",
            1: "24 fps",
            2: "25 fps",
            3: "29.97 fps (NTSC, Non-Drop)",
            4: "29.97 fps (NTSC, Drop-Frame)",
            5: "30 fps",
        }.get(clockType, "Unknown")


class FakeTransportManager(object):
    """Mimics TransportManager -- confirmed live as the source of
    smpteClockType() (see config.py's module docstring)."""

    def __init__(self, clock_type):
        self._clock_type = clock_type

    def smpteClockType(self):
        return self._clock_type


class FakeGuiSystem(object):
    """Mimics guisystem. Defaults to clockType 3 (Timecode.SMPTE2997 --
    29.97fps, non-drop) to match the live D3 project this feature was
    built and validated against."""

    def __init__(self, clock_type=Timecode.SMPTE2997):
        self.currentTransportManager = FakeTransportManager(clock_type)


FAKE_GUISYSTEM = FakeGuiSystem()


class FakeGuardedAction(object):
    """Simulates disguise object-model types (observed: 'Action') that raise
    a TypeError as soon as anything touches repr()/callable() on them. Used
    to prove the 'inspect' expression template survives that landmine
    instead of crashing the whole Discovery Console request (see the
    comment on the 'inspect' template in config.py for the real-world error
    this reproduces)."""

    def __repr__(self):
        raise TypeError("Access to object of type 'Action' is not allowed.")

    def __call__(self, *args, **kwargs):
        raise TypeError("Access to object of type 'Action' is not allowed.")


class FakeD3Project(object):
    """`d3` is a generic Project *resource* -- rename/duplicate/tags/etc --
    not a tracks/library/mappings namespace (confirmed live: a real attempt
    at `d3.project` raised AttributeError, and `dir(d3)` showed ~85 generic
    resource-management attributes instead). Carries a guarded landmine
    attribute so the Discovery Console's safety code stays exercised."""

    def __init__(self):
        self.projectName = "mock_project"
        self.majorVersion = "r32.4.11"
        self.someGuardedThing = FakeGuardedAction()


class ResourceManager(object):
    def __init__(self, tracks, clips, projections):
        self._by_type = {Track: tracks, VideoClip: clips, Projection: projections}

    def allResources(self, cls):
        return list(self._by_type.get(cls, []))

    def load(self, path_str, expectedType=Track):
        for r in self._by_type.get(expectedType, []):
            if str(r.path) == path_str:
                return r
        raise RuntimeError("Unable to find Resource node {}".format(path_str))


DEFAULT_FILENAMES = [
    "042_CityScape_Intro_1_01.mov",
    "042_CityScape_Intro_2_01.mov",
    "042_CityScape_Intro_3_01.mov",
    "042_CityScape_Intro_3_02.mov",  # newer version, should win
    "017_Fireworks_Finale_1_01.mov",
    "017_Fireworks_Finale_2_01.mov",
    "099_Logo_Still_1_01.png",
    "not_a_matching_filename.mov",
    # No explicit version -- D3 manages versioning itself and reports the
    # filename without it (confirmed live); mapping identifier can be a
    # letter code or a plain number in this form. See naming.py.
    "1000_walkin_a.png",
    "1000_walkin_b.png",
    "1020_name2_169.png",
]

FAKE_D3 = FakeD3Project()
FAKE_TRACKS = [
    Track("Main Show", 300.0),
    Track("Video 1", 120.0),
    Track("Video 2", 120.0),
    Track("Audio", 300.0),
    Track("Preshow", 60.0),
]
FAKE_CLIPS = [VideoClip(fn) for fn in DEFAULT_FILENAMES]
FAKE_PROJECTIONS = [Projection(n) for n in ["US Screen", "SR Screen", "SL Screen", "Floor"]]
FAKE_RESOURCE_MANAGER = ResourceManager(FAKE_TRACKS, FAKE_CLIPS, FAKE_PROJECTIONS)


def ok_status():
    return {"code": 0, "message": "ok", "details": []}


def find_track(uid=None, name=None):
    for tr in FAKE_TRACKS:
        if uid and tr.uid == uid:
            return tr
        if name and tr.description == name:
            return tr
    return None


# ---- REST API (confirmed shapes, see module docstring) ---------------------


@app.route("/api/session/transport/tracks", methods=["GET"])
def transport_tracks():
    # REST's "name" field is the Track resource's .description -- see the
    # Track class docstring above.
    result = [
        {"uid": tr.uid, "name": tr.description, "length": tr.length, "crossfade": "Off"} for tr in FAKE_TRACKS
    ]
    return jsonify({"status": ok_status(), "result": result})


@app.route("/api/session/transport/annotations", methods=["GET"])
def transport_annotations():
    uid = request.args.get("uid")
    name = request.args.get("name")
    track = find_track(uid=uid, name=name)
    if track is None:
        return jsonify({"status": {"code": 5, "message": "track not found", "details": []}}), 200
    result = {"notes": [], "tags": [], "sections": track.sections}
    return jsonify({"status": ok_status(), "result": result})


# ---- Python Execution API (confirmed request/response shape only) ----------


@app.route("/api/session/python/execute", methods=["POST"])
def execute():
    body = request.get_json(force=True) or {}
    script = body.get("script", "")

    indented = textwrap.indent(script, "    ")
    func_src = "def __d3_wrapped__():\n" + indented + "\n"

    # The real D3 server runs Python 2.7; this mock runs on Python 3 (this
    # sandbox has no Python 2 interpreter), so shim the handful of Python
    # 2-only builtins the default expression templates reference. Templates
    # you write for the real server can keep using unicode/long normally.
    namespace = {
        "d3": FAKE_D3,
        "resourceManager": FAKE_RESOURCE_MANAGER,
        "guisystem": FAKE_GUISYSTEM,
        "Track": Track,
        "VideoClip": VideoClip,
        "Projection": Projection,
        "VariableVideoModule": VariableVideoModule,
        "Timecode": Timecode,
        "unicode": str,
        "long": int,
    }
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(func_src, "<script>", "exec"), namespace)
            return_value = namespace["__d3_wrapped__"]()
    except Exception as e:  # noqa: BLE001 - mirroring "any script error" from the real API
        return jsonify(
            {
                "status": {"code": 1, "message": "{}: {}".format(type(e).__name__, e), "details": []},
                "d3Log": "",
                "pythonLog": buf.getvalue(),
                "returnValue": None,
            }
        )

    # Confirmed by d3api.swagger.json: returnValue is a JSON *string* on the
    # wire, not a raw JSON value -- mirror that here.
    return jsonify(
        {
            "status": ok_status(),
            "d3Log": "",
            "pythonLog": buf.getvalue(),
            "returnValue": json.dumps(return_value),
        }
    )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    app.run(host="127.0.0.1", port=port)
