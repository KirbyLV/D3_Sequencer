# D3 Sequencer

A local web app for browsing content loaded on a disguise/D3 media server and
inserting layers (and marking a matching Section) at a chosen point on a
Track, driven by an asset-ID based file naming convention.

It talks to D3's own HTTP APIs, so it runs on your machine and needs network
access to your D3 box (e.g. `10.10.22.101`) -- it can't run anywhere that
isn't on the same network as the media server.

## Naming convention

```
{assetID}_{description}_{mappingNumber}_{version}.ext
```

Example: `042_CityScape_Intro_3_v02.mov` -> asset id `042`, description
`CityScape_Intro`, mapping number `3`, version `02`. The description may
contain underscores. `.mov` and common image extensions are both recognized.

Files that don't match the convention are listed separately in the UI
rather than silently dropped.

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

On the **Setup** tab:

1. Enter your D3 server's host/port (defaults to `10.10.22.101` / `80`) and
   **Save Server Settings**, then **Test Connection** (top right).
2. **Scan Filenames** to pull the mapping numbers actually present in your
   media library's file names.
3. **Fetch Mappings from D3** to pull the list of real Projection/mapping
   names from your current show.
4. For each mapping number, click into its row and start typing -- pick the
   real mapping from the suggestions, then **Save Mapping Configuration**.
   This mapping-number -> mapping assignment is global (mapping `3` always
   means the same thing regardless of which asset you're inserting).

### Starting a new show

Mapping assignments point at a specific project's mappings, so they don't
carry over to a new show. After you **Fetch Mappings from D3** on a new
show, any row whose saved assignment isn't found in the new show's mapping
list turns red -- reassign those rows, or click **Clear All (New Show)** to
wipe the table and start over, then **Save Mapping Configuration** when
you're done.

## Using it

On the **Browse & Insert** tab:

1. **Refresh** the media library, or use the search box to filter by asset
   id, description, or filename. Assets are grouped by asset ID; each shows
   its available mapping numbers as chips (a chip shows "(unmapped!)" if
   that mapping number isn't configured yet on Setup).
2. Click an asset card to select it; uncheck any mapping-number chips you
   don't want included in this insert (all are checked by default -- this
   lets you build multiple layers from one asset ID in a single click).
3. Pick the destination **Track**. Enter a **Start** position as timecode
   (`M:SS`, `M:SS:FF`, or `H:MM:SS:FF` -- periods work the same as colons,
   e.g. `1.45.00`, for quick numeric-keypad entry). The **+15s** button
   advances Start by 15 seconds. Existing sections on the selected track are
   shown for reference, and the preview line above Start shows exactly where
   this will land and at what frame rate.
4. Set **Length** in seconds, either by typing a value or with the preset
   buttons (10s / 15s / 30s / 1m). The end point is computed the same
   frame-accurate way as Start, so it lands exactly where it should relative
   to the timeline rather than drifting by a frame.
5. Set the **Mode** toggle to Normal or Locked, and check "Also mark a
   Section here" if you want a Section split at the same point (Sections
   have no name field on this system, so there's nothing to label it with).
6. Click **Insert Looped** or **Insert Paused**.
7. Each selected mapping number becomes its own layer, all starting at the
   same point, each pointing at the newest version of that mapping's file
   and assigned to the mapping/mode/end-point you configured.

The **Result** panel shows a per-layer success/failure breakdown so a
partial failure (e.g. one mapping number's file went missing) is obvious
rather than silent.

## If Designer updates change something

The API calls this app makes (tracks, sections, media, mappings, layer
creation) are stored as editable templates on the **Setup** tab under
**Advanced: API Expression Templates** -- if a Designer update breaks one,
you can fix it there without touching code:

1. Open the **Discovery Console** tab and use "Inspect an object" or "Run
   raw script" to find the corrected call (e.g. inspect `d3` or
   `resourceManager`).
2. Paste the corrected snippet into the matching template under **Setup ->
   Advanced: API Expression Templates** and save -- no restart needed.
3. **Restore Default Expression Templates** discards your edits and reverts
   to the app's built-in version if you want to start over.

## A security note

This app has no authentication of its own and can read/write anything the D3
Execution API allows on your project, so keep it on a network you trust --
the same standard you'd apply to Designer's own Execution API.

## Project layout

- `app.py` -- Flask routes
- `d3_client.py` -- D3 API helpers
- `config.py` / `config.json` (gitignored, created on first run) -- server
  address, mapping-number config, expression templates
- `naming.py` -- filename convention parser/grouping
- `templates/index.html`, `static/` -- the single-page UI
- `tests/` -- mock D3 server + smoke test, for verifying changes before they
  reach a real system
