"""Continuous animation of Stim's exact detector-slice SVG diagrams."""

from __future__ import annotations

import atexit
import html
import math
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import PathLike
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit

import stim

from ._animation import compile_animation as _compile_animation


class _MemoryServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _MemoryHandler)
        self.contents: dict[str, bytes] = {}
        self.contents_lock = Lock()
        self.daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _MemoryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _MemoryServer)
        fields = urlsplit(self.path).path.split("/")
        token = fields[2] if len(fields) == 3 and fields[1] == "animation" else ""
        with server.contents_lock:
            content = server.contents.get(token)
        if content is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


_server: _MemoryServer | None = None
_server_thread: Thread | None = None
_server_lock = Lock()


def _get_server() -> _MemoryServer:
    global _server
    global _server_thread
    with _server_lock:
        if _server is None:
            _server = _MemoryServer()
            _server_thread = Thread(
                target=_server.serve_forever,
                kwargs={"poll_interval": 0.1},
                daemon=True,
                name="dynamic-detection-regions",
            )
            _server_thread.start()
        return _server


def _release(token: str) -> None:
    server = _server
    if server is None:
        return
    with server.contents_lock:
        server.contents.pop(token, None)


def _shutdown_server() -> None:
    global _server
    global _server_thread
    with _server_lock:
        server = _server
        thread = _server_thread
        _server = None
        _server_thread = None
    if server is None:
        return
    with server.contents_lock:
        server.contents.clear()
    server.shutdown()
    server.server_close()
    if thread is not None:
        thread.join(timeout=1)


atexit.register(_shutdown_server)


def _reusable_filter_coords(filter_coords: object) -> object:
    if filter_coords is None or isinstance(filter_coords, (str, stim.DemTarget)):
        return filter_coords
    try:
        entries = tuple(filter_coords)
    except TypeError:
        return filter_coords

    reusable = []
    for entry in entries:
        if isinstance(entry, (str, stim.DemTarget)):
            reusable.append(entry)
            continue
        try:
            reusable.append(tuple(entry))
        except TypeError:
            reusable.append(entry)
    return tuple(reusable)


class _Animation:
    """An in-memory HTML animation that displays inside a notebook."""

    def __init__(self, content: bytes, build_seconds: float, height: int):
        self._content = content
        self._build_seconds = build_seconds
        self._height = height
        self._token: str | None = None
        self._url: str | None = None
        self._display_lock = Lock()
        self._closed = False

    @property
    def build_seconds(self) -> float:
        """Seconds spent asking Stim for frames and compiling transitions."""
        return self._build_seconds

    @property
    def memory_mb(self) -> float:
        """Size of the self-contained HTML payload in decimal megabytes."""
        return len(self._content) / 1_000_000

    @property
    def closed(self) -> bool:
        return self._closed

    def save(self, path: str | PathLike[str]) -> Path:
        """Writes an optional portable HTML copy and returns its path."""
        self._check_open()
        destination = Path(path).expanduser().resolve()
        destination.write_bytes(self._content)
        return destination

    def close(self) -> None:
        """Releases this animation's in-memory HTML."""
        if self._closed:
            return
        with self._display_lock:
            self._closed = True
            if self._token is not None:
                _release(self._token)
            self._content = b""
            self._url = None
            self._token = None

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("This dynamic detection-regions animation is closed.")

    def _notebook_url(self) -> str:
        self._check_open()
        with self._display_lock:
            self._check_open()
            if self._url is None:
                server = _get_server()
                token = secrets.token_urlsafe(24)
                with server.contents_lock:
                    server.contents[token] = self._content
                self._token = token
                self._url = f"http://127.0.0.1:{server.server_port}/animation/{token}"
            return self._url

    def __enter__(self) -> _Animation:
        self._check_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __str__(self) -> str:
        self._check_open()
        return self._content.decode("utf-8")

    def __repr__(self) -> str:
        if self._closed:
            return "<dynamic_detection_regions._Animation closed>"
        return (
            "<dynamic_detection_regions._Animation "
            f"built_in={self.build_seconds:.3f}s size={self.memory_mb:.1f}MB>"
        )

    def _repr_html_(self) -> str:
        if self._closed:
            return "<p>This dynamic detection-regions animation is closed.</p>"
        try:
            url = html.escape(self._notebook_url(), quote=True)
        except OSError:
            document = html.escape(str(self), quote=True)
            return (
                f'<iframe srcdoc="{document}" width="100%" height="{self._height}" '
                'style="border:1px solid #bbb;border-radius:10px;background:white" '
                "allowfullscreen></iframe>"
            )
        return f"""
        <div style="font-family:system-ui,sans-serif">
          <div style="display:flex;gap:14px;align-items:center;margin:4px 0 10px">
            <a href="{url}" target="_blank" style="font-weight:700">Open full-window ↗</a>
            <span style="color:#666;font-size:12px">
              Built in {self.build_seconds:.3f}s · {self.memory_mb:.1f} MB in memory
            </span>
          </div>
          <iframe src="{url}" width="100%" height="{self._height}"
                  style="border:1px solid #bbb;border-radius:10px;background:white"
                  allowfullscreen></iframe>
        </div>
        """


def dynamic_detection_regions(
    circuit: stim.Circuit,
    *,
    tick: int | range | None = None,
    filter_coords: object = None,
    seconds_per_tick: float = 0.55,
    height: int = 820,
    autoplay: bool = True,
    loop: bool = True,
) -> _Animation:
    """Shows a sequence of Stim's combined operation-and-detector diagrams.

    Integer frames are the exact SVGs produced by Stim for the displayed
    diagram-tick argument. Between integer frames, detector boundaries are
    smoothly interpolated by detector identity. The operation layer comes from
    the destination Stim frame. At the terminal diagram tick, a private circuit
    copy with one empty trailing TICK avoids a Stim 1.15/1.16 rendering stall;
    the input circuit is not mutated.

    Args:
        circuit: The Stim circuit to animate.
        tick: A diagram tick or a half-open range of diagram ticks. An integer
            selects one exact Stim frame. Diagram tick k shows the operations
            in window k and the detector slice at boundary k+1. Defaults to all
            diagram ticks from 0 through circuit.num_ticks, inclusive.
        filter_coords: Detector and observable filters accepted by
            stim.Circuit.diagram. When this is given and tick is omitted, the
            animation is trimmed to one frame before the first visible region
            through one frame after the last visible region.
        seconds_per_tick: Playback time between consecutive diagram ticks.
        height: Height in pixels of the notebook iframe.
        autoplay: Whether playback starts when the notebook output appears.
        loop: Whether playback restarts after reaching the final frame.
    """
    if not isinstance(circuit, stim.Circuit):
        raise TypeError("circuit must be a stim.Circuit")
    if not math.isfinite(seconds_per_tick) or seconds_per_tick <= 0:
        raise ValueError("seconds_per_tick must be finite and positive")
    if isinstance(height, bool) or not isinstance(height, int):
        raise TypeError("height must be an int")
    if height < 240:
        raise ValueError("height must be at least 240")
    if not isinstance(autoplay, bool):
        raise TypeError("autoplay must be a bool")
    if not isinstance(loop, bool):
        raise TypeError("loop must be a bool")

    trim_to_filtered_lifetime = tick is None and filter_coords is not None
    filter_coords = _reusable_filter_coords(filter_coords)
    if tick is None:
        tick = range(0, circuit.num_ticks + 1)
    elif isinstance(tick, int) and not isinstance(tick, bool):
        tick = range(tick, tick + 1)
    elif not isinstance(tick, range):
        raise TypeError("tick must be an int or range")
    if tick.step != 1:
        raise ValueError("tick.step != 1")
    if tick.stop <= tick.start:
        raise ValueError("tick.stop <= tick.start")
    if tick.start < 0 or tick.stop > circuit.num_ticks + 1:
        raise ValueError(
            f"tick must be within range(0, {circuit.num_ticks + 1})"
        )

    result = _compile_animation(
        circuit,
        ticks=tick,
        filter_coords=filter_coords,
        trim_to_filtered_lifetime=trim_to_filtered_lifetime,
        seconds_per_tick=seconds_per_tick,
        autoplay=autoplay,
        loop=loop,
    )
    return _Animation(result.content, result.seconds, height)


__all__ = ["dynamic_detection_regions"]
