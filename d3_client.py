"""
Client for the disguise/D3 HTTP APIs.

Two separate APIs are in play, of very different reliability:

1. The REST/gRPC-gateway API described by the live system's
   d3api.swagger.json (basePath /api/session, swagger 2.0). This is a real,
   CONFIRMED contract -- no guessing. It covers status/transport/etc. but has
   NO endpoints for listing media/mappings or creating layers/sections.
   Confirmed from it: there is no "Timeline" object above "Track" -- a
   Project directly contains Tracks (GET /transport/tracks), and
   Sections/Notes/Tags are per-Track "annotations"
   (GET /transport/annotations?name=<track>).
2. The Python Execution API (POST /api/session/python/execute, body
   {"script": "<python 2.7 source>"}, response
   {"status": {"code", "message", "details"}, "d3Log", "pythonLog",
   "returnValue"} -- confirmed shape via the swagger file's pythonExecuteRequest
   /pythonExecuteResponse definitions). This is used for everything the REST
   API doesn't cover: media/mapping listing and layer/section creation.
   Scripts run inside Designer's embedded Python 2.7 interpreter in a
   function scope (so a bare top-level `return` works, and there's no
   persistent state between calls). The object model reached this way is
   now CONFIRMED (official d3.pyi type stubs + live read/write testing) --
   `d3` itself is the Project resource, not a namespace; everything else is
   reached via `resourceManager.allResources(<Class>)`. See config.py's
   module docstring for the full trail.

Everything sent via the Python Execution API is generated from the
"expressions" templates in config.json (see config.py) so the exact
object-model calls can still be corrected, per-installation (e.g. after a
Designer upgrade changes something), without touching this file. Use
D3Client.inspect() / the app's Discovery Console to find the right calls for
your system.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


class D3Error(Exception):
    """Base class for all D3-related errors."""


class D3ConnectionError(D3Error):
    """Could not reach the D3 server at all (network/timeout/bad response)."""


class D3ScriptError(D3Error):
    """A /python/execute script failed (status.code != 0)."""

    def __init__(self, message: str, status: Optional[dict] = None, python_log: str = "", d3_log: str = ""):
        super().__init__(message)
        self.status = status or {}
        self.python_log = python_log
        self.d3_log = d3_log


class D3RestError(D3Error):
    """A REST API call failed (status.code != 0, or a non-200 HTTP status)."""

    def __init__(self, message: str, status: Optional[dict] = None):
        super().__init__(message)
        self.status = status or {}


@dataclass
class ExecResult:
    return_value: Any
    python_log: str
    d3_log: str
    raw: dict


def py_literal(value: Any) -> str:
    """Render a Python value as source text safe to splice into a Python
    2.7-compatible script. Strings/dicts/lists/numbers/bools/None all round
    -trip through JSON syntax, which is valid Python literal syntax too, and
    JSON escaping avoids quote/injection issues that naive string
    interpolation would have."""
    return json.dumps(value)


class D3Client:
    def __init__(self, host: str, port: int = 80, scheme: str = "http", timeout_seconds: float = 15.0):
        self.host = host
        self.port = port
        self.scheme = scheme
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_config(cls, cfg: dict) -> "D3Client":
        s = cfg["server"]
        return cls(
            host=s["host"],
            port=s.get("port", 80),
            scheme=s.get("scheme", "http"),
            timeout_seconds=s.get("timeout_seconds", 15),
        )

    @property
    def base_url(self) -> str:
        return "{}://{}:{}".format(self.scheme, self.host, self.port)

    @property
    def execute_url(self) -> str:
        return "{}/api/session/python/execute".format(self.base_url)

    def rest_url(self, path: str) -> str:
        """path like '/transport/tracks' -- basePath /api/session is confirmed
        by d3api.swagger.json."""
        return "{}/api/session{}".format(self.base_url, path)

    # -- low level: REST -------------------------------------------------

    def rest_get(self, path: str, params: Optional[dict] = None) -> Any:
        url = self.rest_url(path)
        try:
            resp = requests.get(url, params=params, timeout=self.timeout_seconds)
        except requests.exceptions.ConnectTimeout as e:
            raise D3ConnectionError(
                "Timed out connecting to D3 at {}. Is Designer running and reachable on your network?".format(
                    self.base_url
                )
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise D3ConnectionError("Could not connect to D3 at {}. Check the host/port in Setup.".format(self.base_url)) from e
        except requests.exceptions.Timeout as e:
            raise D3ConnectionError("D3 did not respond in time ({}s).".format(self.timeout_seconds)) from e

        if resp.status_code != 200:
            raise D3ConnectionError("D3 returned HTTP {} from {}: {}".format(resp.status_code, url, resp.text[:500]))

        try:
            data = resp.json()
        except ValueError as e:
            raise D3ConnectionError("D3's response wasn't valid JSON (got: {}...).".format(resp.text[:200])) from e

        status = data.get("status") or {}
        if status.get("code", 0) != 0:
            raise D3RestError("D3 REST call to {} failed: {}".format(path, status.get("message", "unknown error")), status=status)

        return data.get("result")

    # -- low level: Python Execution API -----------------------------------

    def run_script(self, script: str) -> ExecResult:
        try:
            resp = requests.post(
                self.execute_url,
                json={"script": script},
                timeout=self.timeout_seconds,
            )
        except requests.exceptions.ConnectTimeout as e:
            raise D3ConnectionError(
                "Timed out connecting to D3 at {}. Is Designer running and reachable on your network?".format(
                    self.base_url
                )
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise D3ConnectionError(
                "Could not connect to D3 at {}. Check the host/port in Setup and that Designer's "
                "Execution API is enabled.".format(self.base_url)
            ) from e
        except requests.exceptions.Timeout as e:
            raise D3ConnectionError("D3 did not respond in time ({}s).".format(self.timeout_seconds)) from e

        if resp.status_code != 200:
            raise D3ConnectionError(
                "D3 returned HTTP {} from {}: {}".format(resp.status_code, self.execute_url, resp.text[:500])
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise D3ConnectionError(
                "D3's response wasn't valid JSON (got: {}...). Double-check the host/port.".format(resp.text[:200])
            ) from e

        status = data.get("status") or {}
        python_log = data.get("pythonLog", "") or ""
        d3_log = data.get("d3Log", "") or ""

        if status.get("code", 0) != 0:
            raise D3ScriptError(
                "D3 script failed: {}".format(status.get("message", "unknown error")),
                status=status,
                python_log=python_log,
                d3_log=d3_log,
            )

        # Confirmed by d3api.swagger.json: returnValue is always a JSON string
        # on the wire (the server json.dumps()'s whatever your script
        # returned), so it needs decoding back into a real value here.
        return_value = data.get("returnValue")
        if isinstance(return_value, str):
            try:
                return_value = json.loads(return_value)
            except (ValueError, TypeError):
                pass

        return ExecResult(return_value=return_value, python_log=python_log, d3_log=d3_log, raw=data)

    def test_connection(self) -> ExecResult:
        return self.run_script("return {'ok': True}")

    # -- REST-backed helpers (confirmed by d3api.swagger.json) -----------

    def list_tracks(self) -> List[dict]:
        """GET /transport/tracks -- confirmed endpoint, no scripting involved.
        Each item has 'uid', 'name', 'length' (seconds), 'crossfade'."""
        result = self.rest_get("/transport/tracks")
        return result if isinstance(result, list) else []

    def list_sections(self, track_name: str) -> List[dict]:
        """GET /transport/annotations?name=<track> -- confirmed endpoint.
        Returns just the 'sections' list (notes/tags are also available in
        the same response but aren't used by this app)."""
        result = self.rest_get("/transport/annotations", params={"name": track_name})
        if isinstance(result, dict):
            return result.get("sections", [])
        return []

    # -- template-driven helpers (Python Execution API, unconfirmed object model) --

    def inspect(self, expressions: Dict[str, str], target_expr: str) -> ExecResult:
        script = expressions["inspect"].format(target_expr=target_expr)
        return self.run_script(script)

    def list_media(self, expressions: Dict[str, str]) -> List[dict]:
        result = self.run_script(expressions["media_list"])
        return _as_list(result.return_value)

    def list_mappings(self, expressions: Dict[str, str]) -> List[dict]:
        result = self.run_script(expressions["mapping_list"])
        return _as_list(result.return_value)

    def create_layer(
        self,
        expressions: Dict[str, str],
        *,
        track_name: str,
        start_seconds: float,
        length_seconds: float,
        layer_name: str,
        resource_path: str,
        mapping_resource_path: str,
        mode: str,
        end_mode: str,
    ) -> dict:
        """start_seconds/length_seconds are plain seconds -- the
        create_layer template converts to track beats on the D3 side via
        track.timeToBeat(), which is the only way to do that correctly if
        the track has a tempo map (see config.py's module docstring).
        resource_path/mapping_resource_path are the stable str(path)
        identifiers returned by list_media()/list_mappings()."""
        script = expressions["create_layer"].format(
            track_name=py_literal(track_name),
            start_seconds=py_literal(start_seconds),
            length_seconds=py_literal(length_seconds),
            layer_name=py_literal(layer_name),
            resource_path=py_literal(resource_path),
            mapping_resource_path=py_literal(mapping_resource_path),
            mode=py_literal(mode),
            end_mode=py_literal(end_mode),
        )
        result = self.run_script(script)
        out = (
            result.return_value
            if isinstance(result.return_value, dict)
            else {"ok": True, "raw": result.return_value}
        )

        # Diagnostics: always attach exactly what was sent, and -- via a
        # SEPARATE follow-up script execution, so it reflects a fresh
        # read from D3 rather than anything cached from the create call --
        # what the layer's module fields actually ended up holding
        # immediately afterward. This is here because of a real bug where
        # the create call reported ok=True with the right name/position but
        # the video/mapping silently stayed at their untouched defaults.
        out["_script_sent"] = script
        out["_create_pythonLog"] = result.python_log
        out["_create_d3Log"] = result.d3_log
        if out.get("ok") and "verify_layer" in expressions:
            try:
                verify_script = expressions["verify_layer"].format(
                    track_name=py_literal(track_name),
                    layer_name=py_literal(layer_name),
                )
                verify_result = self.run_script(verify_script)
                out["_verify"] = verify_result.return_value
            except D3Error as e:
                out["_verify"] = {"found": False, "reason": "verify_layer call itself failed: {}".format(e)}
        return out

    def get_timecode_info(self, expressions: Dict[str, str]) -> dict:
        """Returns {'clockType': int, 'fps': float} describing the
        project's current SMPTE timecode display setting -- see
        config.py's module docstring ("Timecode / NTSC frame rates") for
        the confirmed clockType values (0..5, one per Timecode.SMPTE*
        constant) and how this was validated live."""
        script = expressions["timecode_info"].format()
        result = self.run_script(script)
        return result.return_value if isinstance(result.return_value, dict) else {}

    def timecode_to_seconds(self, expressions: Dict[str, str], timecode: str) -> dict:
        """Converts an already-normalized 'HH:MM:SS:FF' timecode string
        (see timecode_util.normalize_timecode -- call that FIRST; this
        method does no format flexibility of its own) to real elapsed
        seconds, using D3's own Timecode.fromString() and the project's
        live SMPTE clock type. Returns {'seconds': float, 'clockType':
        int}. This is the whole point of doing it this way instead of
        hand-rolling NTSC/drop-frame math on our side: D3 already knows
        exactly how it maps real time to the numbers on its own timeline
        ruler, for whatever clock type the project actually has -- see
        config.py's module docstring for the live data this was
        confirmed against."""
        script = expressions["timecode_to_seconds"].format(timecode=py_literal(timecode))
        result = self.run_script(script)
        out = result.return_value if isinstance(result.return_value, dict) else {}
        if "seconds" not in out:
            raise D3ScriptError(
                "timecode_to_seconds didn't return a 'seconds' value -- got: {!r}".format(result.return_value)
            )
        return out

    def create_section(
        self,
        expressions: Dict[str, str],
        *,
        track_name: str,
        start_seconds: float,
    ) -> dict:
        """Sections have no name/label field on this system (confirmed via
        both the REST schema and d3.pyi's SectionInfo class) -- this just
        splits a new Section at start_seconds, converted to beats on the D3
        side the same way create_layer does."""
        script = expressions["create_section"].format(
            track_name=py_literal(track_name),
            start_seconds=py_literal(start_seconds),
        )
        result = self.run_script(script)
        return result.return_value if isinstance(result.return_value, dict) else {"ok": True, "raw": result.return_value}


def _as_list(value: Any) -> List[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise D3Error("Expected a list back from D3 but got: {!r}".format(value))
