"""Animate a Stim circuit's exact detection regions inside a notebook."""

from __future__ import annotations

import html
import sys
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import PathLike
from pathlib import Path
from threading import Thread

import stim

from ._compiler import VectorCompileReport, compile_animation_in_memory


class _MemoryServer(ThreadingHTTPServer):
    content: bytes

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class _MemoryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/animation"}:
            self.send_error(404)
            return
        content = self.server.content  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        pass


@dataclass
class _Animation:
    """The live, in-memory animation returned to a notebook cell."""

    _content: bytes = field(repr=False)
    _report: VectorCompileReport = field(repr=False)
    _server: _MemoryServer = field(repr=False)
    _thread: Thread = field(repr=False)
    _url: str = field(repr=False)
    _height: int = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def build_seconds(self) -> float:
        return self._report.total_compile_seconds

    @property
    def memory_mb(self) -> float:
        return len(self._content) / 1_000_000

    def save(self, path: str | PathLike[str]) -> Path:
        """Optionally saves a portable HTML copy."""
        destination = Path(path).expanduser().resolve()
        destination.write_bytes(self._content)
        return destination

    def close(self) -> None:
        """Releases the animation and its memory."""
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._content = b""

    def __repr__(self) -> str:
        state = "closed" if self._closed else "live"
        return (
            f"Dynamic detection regions ({state}; built in "
            f"{self.build_seconds:.3f}s; {self.memory_mb:.1f} MB in memory)"
        )

    def _repr_html_(self) -> str:
        if self._closed:
            return "<p>This animation is closed.</p>"
        safe_url = html.escape(self._url, quote=True)
        return f"""
        <div style="font-family:system-ui,sans-serif">
          <div style="display:flex;gap:14px;align-items:center;margin:4px 0 10px">
            <a href="{safe_url}" target="_blank" style="font-weight:700">
              Open full-window ↗
            </a>
            <span style="color:#666;font-size:12px">
              Built in {self.build_seconds:.3f}s · {self.memory_mb:.1f} MB in memory
            </span>
          </div>
          <iframe src="{safe_url}" width="100%" height="{self._height}"
                  style="border:1px solid #bbb;border-radius:10px;background:white"
                  allowfullscreen></iframe>
        </div>
        """


_active_animation: _Animation | None = None


def dynamic_detection_regions(
    circuit: stim.Circuit,
    *,
    seconds_per_tick: float = 0.55,
    height: int = 820,
) -> _Animation:
    """Shows exact Stim detection regions moving continuously between ticks."""
    if not isinstance(circuit, stim.Circuit):
        raise TypeError("circuit must be a stim.Circuit")
    if circuit.num_ticks < 1:
        raise ValueError("circuit must contain at least one TICK")
    if height < 240:
        raise ValueError("height must be at least 240")

    global _active_animation
    if _active_animation is not None:
        _active_animation.close()

    content, report = compile_animation_in_memory(
        circuit,
        start_tick=0,
        end_tick=circuit.num_ticks,
        seconds_per_tick=seconds_per_tick,
    )
    server = _MemoryServer(("127.0.0.1", 0), _MemoryHandler)
    server.daemon_threads = True
    server.content = content
    thread = Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        daemon=True,
    )
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/animation?v={report.sha256[:12]}"
    animation = _Animation(content, report, server, thread, url, height)
    _active_animation = animation
    return animation


__all__ = ["dynamic_detection_regions"]
