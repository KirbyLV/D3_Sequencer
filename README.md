# D3 Sequencer

A local web app for browsing content loaded on a disguise/D3 media server and
inserting layers (and marking a matching Section) at a chosen point on a
Track, driven by an asset-ID based file naming convention.

It talks to two of D3's HTTP APIs, so it runs on your own machine and needs
network access to your D3 box (e.g. `172.16.16.107`) -- it cannot run inside
a sandboxed environment that isn't on your LAN.

## Status: confirmed against a live system

Every API call this app makes has now been confirmed against a real show
(Designer r32.4.11, build 253994) -- first via the live system's own
`d3api.swagger.json` and the official disguise Python type stubs
(`d3.pyi`, from https://developer.disguise.one/assets/d3.pyi), then by one
live, reversible write test: creating a throwaway layer with real
Mapping/Video/Mode/At-end-point values, reading every field back to confirm
it stuck, then removing it (`track.removeLayer(...)`). Nothing in this app
is guesswork anymore.

**Confirmed via `d3api.swagger.json` (REST API, no scripting involved):**
- `POST /python/execute` request/response shape: `{"script": "..."}` in,
  `{"status": {"code", "message", "details"}, "d3Log", "pythonLog",
  "returnValue"}` out. `returnValue` is a JSON string on the wire (the server
  `json.dumps()`'s your script's return value), which `d3_client.py` decodes.
- **There is no "Timeline" object above "Track".** A project directly
  contains Tracks (`GET /transport/tracks` -- name, uid, length in seconds,
  crossfade mode), and Sections/Notes/Tags are per-Track "annotations"
  (`GET /transport/annotations?name=<track>`). `d3_client.py`'s
  `list_tracks()`/`list_sections()` call these two REST endpoints directly.

**Confirmed via `d3.pyi` + live read/write testing (Python Execution API):**
- `d3` itself is the current **Project resource** -- not a namespace with
  `.tracks`/`.library`/`.mappings` (a live attempt at `d3.project` raised
  `AttributeError: 'D3' object has no attribute 'project'`, and `dir(d3)`
  showed ~85 generic resource-management attributes like `rename`,
  `duplicate`, `lock`, `tags`, `projectName` instead). Everything else is
  reached via the global `resourceManager.allResources(<Class>)`, which
  enumerates every live resource of a given type.
- **Tracks:** `resourceManager.allResources(Track)`.
- **Media ("Video"):** a layer's module assigns a **VideoClip** resource
  (`resourceManager.allResources(VideoClip)`), not a raw filename. Each
  clip's `.path` is a structured object; `.path.filename` is the actual
  on-disk filename the naming-convention parser reads, and `str(.path)` is
  a stable identifier `resourceManager.load(path_str, VideoClip)` can fetch
  again later (round-trips correctly -- confirmed live).
- **Mappings:** what Designer's layer inspector calls "Mapping" is a
  **Projection** resource (`resourceManager.allResources(Projection)`),
  *not* the `LedScreen` objects on the Stage -- those are the underlying
  physical/virtual screens a Projection targets (`projection.screens`).
  `.description` is the human-readable name shown in Designer (this is what
  matched the numbered mapping names in your show, e.g. `01_vignette_a`,
  `full-canvas`, `tracked_full-canvas`); `str(.path)` is the stable
  identifier, same pattern as VideoClip.
- **A video layer's module is a `VariableVideoModule`** (not
  `d3.VideoModule` -- that name doesn't exist on this system). Confirmed
  properties, straight from `d3.pyi`'s docstrings and a live round-trip:
  - `.video` -- a VideoClip (this is disguise's "Video" field)
  - `.mapping` -- a Projection (this is disguise's "Mapping" field)
  - `.mode` -- int, `{0: 'Locked', 1: 'Normal'}`
  - `.at_end_point` -- int, `{0: 'Loop', 1: 'Ping-pong', 2: 'Pause'}`
- **Creating a layer:** `track.addNewLayer(VariableVideoModule, tStart,
  tLength, name)`. `tStart`/`tLength` are in **track beats**, confirmed by
  disguise's own Track & Sequencing guide -- convert from the seconds this
  app's UI uses with `track.timeToBeat(seconds)` (confirmed present on
  Track; this is the only correct way to do the conversion if a track has a
  tempo map, rather than assuming a fixed beats-per-second ratio).
- **Sections have no name/label field.** Confirmed both by the REST schema
  (`transportSectionInfo`) and by `d3.pyi`'s `SectionInfo` class (`iSection`,
  `tStart`, `tEnd`, `transition` only -- no name). They're created with
  `track.splitSectionAtBeat(beat)`, which splits/marks a Cue as a Section at
  that beat; there is no `addSection(name, start)` call. This app can mark a
  Section at your chosen point, but can't attach a label to it -- the
  "Also mark a Section here" checkbox on the Insert tab reflects that.

All of this lives in `config.py`'s `expressions` templates (editable from
the app's **Setup** tab, no code changes needed) -- see that file's module
docstring for the full trail, including two D3-specific error-handling
landmines worth knowing about if you ever edit these:
- Some disguise object-model types (observed: `Action`) raise an
  uncatchable `TypeError` the instant *anything* touches them, even
  `type()`. The fix is to never touch an attribute whose name looks like
  one of these before calling `getattr` on it (the `inspect` template skips
  anything containing "action").
- Calling `getattr(obj, name)` for a name that's valid for *some* objects of
  a general kind but not this specific instance (e.g. assuming every
  layer's `.module` is a `VariableVideoModule` when some are actually
  `TestPatternModule` or `GroupLayer`) raises an `AttributeError` that is
  *also* not reliably catchable by `try/except`. The fix is the same shape:
  check `name in dir(obj)` before calling `getattr`, rather than calling it
  and hoping to catch a failure.

### If Designer changes something in a future version

Everything above is confirmed for r32.4.11, not guaranteed forever. If an
upgrade changes the object model, use the **Discovery Console** tab:

1. `d3` and `resourceManager` are always safe to inspect -- the `inspect`
   template only ever calls `type()`, never `repr()`/`callable()`, for the
   uncatchable-TypeError reason above.
2. Try `resourceManager.allResources(Track)[0]`, `.layers[0]`, `.module`,
   etc. to re-confirm the chain still works the way this README describes.
3. Paste any corrected snippets into **Setup -> Advanced: API Expression
   Templates** and Save -- no restart needed.
4. "Run raw script" on the Console tab runs any one-off script and shows
   `returnValue`/`pythonLog`/`d3Log` -- handy for trying things before
   committing them to a template.

## Naming convention

```
{assetID}_{description}_{mappingNumber}_{version}.ext
```

Example: `042_CityScape_Intro_3_02.mov` -> asset id `042`, description
`CityScape_Intro`, mapping number `3`, version `02`. The description may
contain underscores; the parser (`naming.py`) anchors on the numeric asset
id at the start and the trailing `_<mapping>_<version>.<ext>`, so it handles
that correctly. `.mov` and common image extensions are both recognized.

Files that don't match the convention are listed separately in the UI ("...
files that don't match the naming convention") rather than silently dropped.

Every asset id groups its mapping-number variants together; when a mapping
number has more than one version present, the app always uses the newest
version.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python3 app.py
```

Then open http://127.0.0.1:5151 in a browser.

If you have a `config.json` from before this confirmed object model landed
(anything mentioning `d3.tracks`, `d3.library`, `d3.mappings`, or
`layer.endMode`), delete it so the app regenerates it with the corrected
defaults -- the old templates will fail against a real system.

On the **Setup** tab:

1. Enter your D3 server's host/port (defaults to `172.16.16.107` / `80`) and
   **Save Server Settings**, then **Test Connection** (top right).
2. **Scan Filenames** to pull the mapping numbers actually present in your
   media library's file names.
3. **Fetch Mappings from D3** to pull the list of real Projection/mapping
   names from your show.
4. For each mapping number, click into its row and start typing -- pick the
   real mapping from the suggestions, then **Save Mapping Configuration**.
   (Under the hood this stores the mapping's stable resource path, not just
   its display name, since Designer lets you rename a mapping later.) This
   mapping-number -> mapping assignment is global (mapping `3` always means
   the same thing regardless of which asset you're inserting).

## Using it

On the **Browse & Insert** tab:

1. **Refresh** the media library. Assets are grouped by asset ID; each shows
   its available mapping numbers as chips (a chip shows "(unmapped!)" if
   that mapping number isn't configured yet on Setup).
2. Click an asset card to select it; uncheck any mapping-number chips you
   don't want included in this insert (all are checked by default -- this
   is the "select groups of media assets that share an asset ID and build
   multiple layers with one click" behaviour).
3. Pick the destination **Track**, a **Start** position and **Length** in
   seconds -- the app converts these to track beats on the D3 side via
   `track.timeToBeat()` before creating anything. Existing sections on the
   selected track are shown for reference.
4. Set the **Mode** toggle to Normal or Locked.
5. Click **Insert Looped** or **Insert Paused** for the common cases, or
   pick **ping-pong** from the dropdown and click **Insert (chosen end
   mode)** for the rest. Check "Also mark a Section here" to split a
   Section at the same point (Sections have no name field on this system,
   so there's nothing to label it with -- see Status above).
6. Each selected mapping number becomes its own layer, all starting at the
   same point, each pointing at the newest version of that mapping's file
   and assigned to the mapping/mode/end-point you configured.

The **Result** panel shows a per-layer success/failure breakdown so a
partial failure (e.g. one mapping number's file went missing) is obvious
rather than silent.

## A security note

While confirming the object model, inspecting `d3.password` (the project's
own password field) returned the plaintext password over the Python
Execution API. This may just be a default/placeholder value on your test
system, but worth knowing: anyone who can reach `/api/session/python/execute`
on your D3 box can read that field. Same goes for this app -- it has no
authentication of its own, so it should stay on a network you trust, same as
Designer's Execution API itself.

## Testing

`tests/mock_d3_server.py` is a minimal stand-in for D3's two HTTP APIs. Its
REST endpoints (`/transport/tracks`, `/transport/annotations`) mirror the
confirmed swagger schemas exactly; its `/python/execute` object model
(`resourceManager`, `Track`, `VideoClip`, `Projection`,
`VariableVideoModule`, plus a deliberately "un-repr-able" attribute on `d3`
to test the Discovery Console's safety) mirrors the confirmed real one --
same class names, same attribute names, same option codes.
`tests/run_smoke_test.py` drives every Flask route against it end-to-end --
config persistence, the naming-convention parser/grouping, and the request
wiring:

```bash
python3 tests/run_smoke_test.py
```

This proves everything around the D3 API calls is solid, including that the
Discovery Console survives a landmine attribute instead of crashing, and
that a full insert (media lookup -> layer creation -> section marking)
round-trips correctly through the confirmed object model.

## Project layout

- `app.py` -- Flask routes
- `d3_client.py` -- REST helpers (tracks/sections, confirmed) + Python Execution API wrapper and template-driven helper calls (media/mappings/layer/section, confirmed)
- `config.py` / `config.json` (gitignored, created on first run) -- server address, mapping-number config, expression templates
- `naming.py` -- filename convention parser/grouping
- `templates/index.html`, `static/` -- the single-page UI
- `tests/` -- mock D3 server + smoke test
- `d3api.swagger.json` -- the live system's captured REST API spec (not read by the app at runtime; kept for reference)
- `d3.pyi` -- the official disguise Python API type stubs (not read by the app at runtime; kept for reference when correcting expression templates -- see https://developer.disguise.one/assets/d3.pyi for the latest version)
