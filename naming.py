"""
Filename convention parser for D3 Sequencer.

Convention (version present -- the "spec" form):
    {assetID}_{description}_{mappingNumber}_{version}.{ext}

Example:
    042_CityScape_Intro_3_02.mov
      asset_id   = "042"
      description= "CityScape_Intro"
      mapping_no = "3"
      version    = "02"
      ext        = "mov"

Convention (no version -- confirmed live, see below):
    {assetID}_{description}_{mappingIdentifier}.{ext}

Example:
    1000_walkin_a.png
      asset_id   = "1000"
      description= "walkin"
      mapping_no = "a"
      version    = None
      ext        = "png"

Both forms are tried, version-first, so a filename that genuinely ends in
two underscore-separated digit groups (mapping number + version) still
parses exactly as it always has -- the no-version form only kicks in when
the version-form doesn't match at all, so there's no ambiguity between the
two.

Why two forms exist: disguise/D3 handles asset **versioning itself**
(re-importing a file updates that resource's own internal version instead
of creating a new differently-named one), so a file uploaded as
e.g. "1000_walkin_a_v01.png" gets reported back by D3's media library
*without* the version suffix -- confirmed live, since D3 owns that part of
the filename once it's ingested the asset. The version field in the "spec"
form above is for the (still fully supported) case of hand-managing
versions yourself via the filename.

The mapping identifier in the no-version form isn't always numeric --
confirmed live test filenames include both letter codes (a, b, c, f) and
plain numbers (169) -- so it accepts letters or digits there. The
mapping-number field in the WITH-version form stays digits-only, matching
the original spec.

The description itself may legitimately contain underscores, so parsing
anchors on the asset id at the start and the mapping number (+ version, if
present) at the end, taking everything in between as the description.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Asset id: one or more digits (allows leading zeros, e.g. "007", "42").
# Mapping number: one or more digits, optionally prefixed with 'm' or 'M'.
# Version: one or more digits, optionally prefixed with 'v' or 'V'.
FILENAME_RE_WITH_VERSION = re.compile(
    r"^(?P<asset_id>\d+)_"
    r"(?P<description>.+)_"
    r"[mM]?(?P<mapping_no>\d+)_"
    r"[vV]?(?P<version>\d+)"
    r"\.(?P<ext>[A-Za-z0-9]+)$"
)

# Same shape, but no version field, and the mapping identifier can be
# letters (placeholder/test codes) or digits -- see module docstring.
FILENAME_RE_NO_VERSION = re.compile(
    r"^(?P<asset_id>\d+)_"
    r"(?P<description>.+)_"
    r"[mM]?(?P<mapping_no>[0-9A-Za-z]+)"
    r"\.(?P<ext>[A-Za-z0-9]+)$"
)

IMAGE_EXTS = {"png", "jpg", "jpeg", "tif", "tiff", "tga", "bmp", "exr"}
VIDEO_EXTS = {"mov", "mp4", "mxf", "avi", "mkv"}


@dataclass
class ParsedAsset:
    filename: str
    asset_id: str
    description: str
    mapping_no: str
    version: Optional[str]
    ext: str
    media_type: str  # "video" | "image" | "other"
    raw: Optional[dict] = None  # original record from D3 (path, duration, etc.)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "asset_id": self.asset_id,
            "description": self.description,
            "mapping_no": self.mapping_no,
            "version": self.version,
            "ext": self.ext,
            "media_type": self.media_type,
            "raw": self.raw,
        }


def classify_ext(ext: str) -> str:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def parse_filename(filename: str, raw: Optional[dict] = None) -> Optional[ParsedAsset]:
    """Parse a single filename. Returns None if it doesn't match either form
    of the convention (see module docstring). Tries the WITH-version form
    first so an unambiguous versioned filename is never mis-split by the
    looser no-version form."""
    # Strip any directory component.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]

    m = FILENAME_RE_WITH_VERSION.match(base)
    if not m:
        m = FILENAME_RE_NO_VERSION.match(base)
    if not m:
        return None

    gd = m.groupdict()
    return ParsedAsset(
        filename=base,
        asset_id=gd["asset_id"],
        description=gd["description"],
        mapping_no=gd["mapping_no"],
        version=gd.get("version"),  # None when matched via the no-version form
        ext=gd["ext"],
        media_type=classify_ext(gd["ext"]),
        raw=raw,
    )


@dataclass
class AssetGroup:
    """All variants (one per mapping number, possibly multiple versions) that
    share the same asset id."""

    asset_id: str
    description: str
    # mapping_no -> list of ParsedAsset (usually just the latest version, but
    # we keep every version so the UI/caller can pick).
    variants: Dict[str, List[ParsedAsset]] = field(default_factory=dict)

    def add(self, asset: ParsedAsset) -> None:
        self.variants.setdefault(asset.mapping_no, []).append(asset)

    def latest(self, mapping_no: str) -> Optional[ParsedAsset]:
        items = self.variants.get(mapping_no)
        if not items:
            return None
        return max(items, key=lambda a: _version_key(a.version))

    def latest_by_mapping(self) -> Dict[str, ParsedAsset]:
        return {mn: self.latest(mn) for mn in self.variants if self.latest(mn)}

    def mapping_numbers(self) -> List[str]:
        return sorted(self.variants.keys(), key=_mapping_sort_key)

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "description": self.description,
            "mapping_numbers": self.mapping_numbers(),
            "variants": {
                mn: [a.to_dict() for a in sorted(items, key=lambda a: _version_key(a.version), reverse=True)]
                for mn, items in self.variants.items()
            },
        }


def _version_key(version: Optional[str]):
    # No version (D3-managed versioning -- see module docstring) sorts
    # lowest; there's normally only one variant per mapping number in that
    # case anyway, so this only matters if duplicates somehow show up.
    if version is None:
        return -1
    try:
        return int(version)
    except ValueError:
        return version


def _mapping_sort_key(mn: str) -> Tuple[int, object]:
    """Numeric mapping identifiers sort first, in numeric order; anything
    else (e.g. placeholder letter codes like 'a'/'b'/'c') sorts after,
    alphabetically. Keeps `sorted()` from raising on a mix of the two,
    which real D3-reported filenames can produce -- see module docstring."""
    try:
        return (0, int(mn))
    except ValueError:
        return (1, mn)


def group_assets(filenames: List[str], raw_by_filename: Optional[Dict[str, dict]] = None) -> Dict[str, AssetGroup]:
    """Parse and group a list of filenames (as reported by D3's media library)
    by asset id. Filenames that don't match the convention are ignored here;
    callers should surface them separately (see unparsed_filenames)."""
    raw_by_filename = raw_by_filename or {}
    groups: Dict[str, AssetGroup] = {}
    for fn in filenames:
        parsed = parse_filename(fn, raw=raw_by_filename.get(fn))
        if not parsed:
            continue
        grp = groups.get(parsed.asset_id)
        if grp is None:
            grp = AssetGroup(asset_id=parsed.asset_id, description=parsed.description)
            groups[parsed.asset_id] = grp
        grp.add(parsed)
    return groups


def unparsed_filenames(filenames: List[str]) -> List[str]:
    return [fn for fn in filenames if parse_filename(fn) is None]


def distinct_mapping_numbers(filenames: List[str]) -> List[str]:
    nums = set()
    for fn in filenames:
        parsed = parse_filename(fn)
        if parsed:
            nums.add(parsed.mapping_no)
    return sorted(nums, key=_mapping_sort_key)
