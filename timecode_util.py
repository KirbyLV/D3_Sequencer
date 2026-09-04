"""
Helpers for turning a user-typed timecode string into the canonical
'HH:MM:SS:FF' form that D3's own Timecode.fromString() expects.

This module does ONLY cosmetic/format normalization -- zero-padding,
filling in an omitted hours field, and accepting '.' interchangeably with
':' as the field separator (so it can be typed entirely on a numeric
keypad, which has no ':' key). It never does the actual time-value math
(real seconds <-> frame count, NTSC 1000/1001 scaling, drop-frame
frame-number skipping): that's delegated entirely to D3's own Timecode
class, via the "timecode_to_seconds" / "timecode_info" expressions in
config.py and D3Client.timecode_to_seconds() / get_timecode_info() in
d3_client.py.

Why delegate instead of computing it here: D3 already knows -- for
whatever SMPTE clock type the project is actually set to -- exactly how
its own timeline ruler converts between real elapsed time and the
displayed timecode. Confirmed live against Josh's project (track "track
1", bpm 60, guisystem.currentTransportManager.smpteClockType() == 3,
i.e. Timecode.SMPTE2997 -- 29.97fps, non-drop):

    Timecode(105.0, 3).asString(False)        == "00:01:44.27"
    Timecode.fromString("00:01:45:00", 3).t   == 105.10510751317817

The first line is an exact match for what Josh independently reported
seeing in Designer (105 real seconds lands the layer at displayed
"1:44:27"), which is what confirmed this whole approach. Re-implementing
that conversion by hand here would just create a second place for it to
silently drift out of sync with whatever Designer itself does --
especially for drop-frame clock types (Timecode.SMPTE2997DF == 4),
which have their own frame-number-skipping rules that are easy to get
subtly wrong. D3's Timecode class is the single source of truth; this
module's only job is turning whatever a person types into a string
Timecode.fromString() will accept.

The 6 confirmed SMPTE clock type constants (Timecode.SMPTE23976 == 0,
SMPTE24 == 1, SMPTE25 == 2, SMPTE2997 == 3, SMPTE2997DF == 4,
SMPTE30 == 5) are documented alongside the expressions in config.py.
"""
from __future__ import annotations

import re

_ALLOWED_CHARS = re.compile(r"^[0-9:.\s]+$")


def normalize_timecode(raw: str) -> str:
    """Accepts flexible timecode input and returns a canonical
    'HH:MM:SS:FF' string (each field zero-padded to at least 2 digits).

    '.' and ':' are treated as interchangeable field separators
    throughout -- typed entirely on a numeric keypad (which has no ':'
    key) or copy-pasted from D3's own asString() output (which uses a
    period specifically before the frames field), it makes no
    difference. Accepted input shapes -- any left-off higher field
    defaults to 0, and frames default to 0 if omitted entirely:

        "45"              -> "00:00:45:00"   (bare seconds)
        "1:45" / "1.45"   -> "00:01:45:00"   (MM:SS)
        "1:45:00"         -> "00:01:45:00"   (MM:SS:FF)
        "1.45.00"         -> "00:01:45:00"   (MM:SS:FF, keypad-friendly)
        "1:44:27"         -> "00:01:44:27"   (MM:SS:FF)
        "0:01:45:00"      -> "00:01:45:00"   (HH:MM:SS:FF)
        "00:01:44.27"     -> "00:01:44:27"   (D3's own asString() output
                                               format -- '.' before frames)

    Raises ValueError with a human-readable message if `raw` doesn't
    match any of these shapes. Does NOT validate the frame number
    against the project's actual fps (e.g. rejecting frame 28 on a
    29.97fps/30-frame project) -- that's left to D3's own
    Timecode.fromString(), which is the authority on what's valid for
    the project's actual clock type.
    """
    if raw is None:
        raise ValueError("timecode is required")
    s = raw.strip()
    if not s:
        raise ValueError("timecode is required")
    if not _ALLOWED_CHARS.match(s):
        raise ValueError(
            "Couldn't understand timecode {!r} -- use digits and ':' or '.' only, "
            "e.g. 1:45:00 or 1.45.00".format(raw)
        )
    s = s.replace(" ", "")
    if not s:
        raise ValueError("timecode is required")

    # '.' and ':' are interchangeable separators (see docstring) -- from
    # here on, treat the string as purely colon-separated.
    s = s.replace(".", ":")

    pieces = s.split(":")
    if len(pieces) >= 3:
        # A 3- or 4-piece string always ends in a frames field -- per the
        # convention confirmed against how Josh himself writes these
        # ("1:45:00" means 1 minute 45 seconds 0 frames, i.e. MM:SS:FF,
        # never HH:MM:SS with no frames field at all).
        main, frames_part = ":".join(pieces[:-1]), pieces[-1]
    else:
        main, frames_part = s, "0"

    if frames_part == "" or not frames_part.isdigit():
        raise ValueError("Couldn't understand timecode {!r} -- bad frames field".format(raw))

    time_fields = main.split(":") if main else []
    if not (1 <= len(time_fields) <= 3) or not all(p.isdigit() for p in time_fields):
        raise ValueError(
            "Couldn't understand timecode {!r} -- expected [[HH:]MM:]SS[:FF]".format(raw)
        )

    parsed = [int(p) for p in time_fields]
    while len(parsed) < 3:
        parsed.insert(0, 0)
    hh, mm, ss = parsed
    ff = int(frames_part)

    if mm >= 60 or ss >= 60:
        raise ValueError(
            "Couldn't understand timecode {!r} -- minutes and seconds must each be less than 60".format(raw)
        )

    return "{:02d}:{:02d}:{:02d}:{:02d}".format(hh, mm, ss, ff)


def add_seconds(raw: str, delta_seconds: int) -> str:
    """Adds a whole number of seconds to a timecode's HH:MM:SS fields
    (the frames field is left untouched) and returns the result in the
    same canonical 'HH:MM:SS:FF' form normalize_timecode() produces.
    `raw` accepts anything normalize_timecode() does.

    This is deliberately PURE CLOCK ARITHMETIC on the displayed
    timecode -- not a trip through D3's Timecode class to real seconds,
    add delta_seconds there, and convert back. That route would very
    slightly shift the result on an NTSC clock type, due to the ~0.1%
    real-time drift documented in config.py's module docstring -- barely
    noticeable for one add, but it would compound with every click of a
    "+15s, advance to the next slot" button as you chain inserts down a
    timeline. Adding whole seconds directly to the clock fields is exact
    and frame-rate-independent -- it's what you'd get nudging the ruler
    display by hand -- which is what a button meant to just advance the
    Start field to the next slot should do.

    A result that would go before 0:00:00:00 (a large negative
    delta_seconds) clamps to 0:00:00:00 rather than raising or going
    negative; the original frames field is preserved even when clamped.
    """
    normalized = normalize_timecode(raw)
    hh, mm, ss, ff = (int(p) for p in normalized.split(":"))
    total_seconds = hh * 3600 + mm * 60 + ss + int(delta_seconds)
    if total_seconds < 0:
        total_seconds = 0
    hh, remainder = divmod(total_seconds, 3600)
    mm, ss = divmod(remainder, 60)
    return "{:02d}:{:02d}:{:02d}:{:02d}".format(hh, mm, ss, ff)
