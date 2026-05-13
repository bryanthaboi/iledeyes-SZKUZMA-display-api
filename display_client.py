"""Thin async HTTP client for displayathon-service.

Used by app.py (the NiceGUI window). Keeping all HTTP plumbing in one place
makes it trivial for external scripts to either talk to the API directly with
their preferred client or import this and call typed methods.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx


DEFAULT_BASE_URL = os.environ.get("DISPLAYATHON_URL", "http://127.0.0.1:49696")


@dataclass
class Result:
    ok: bool
    message: str
    status: int
    meta: dict


class ServiceUnreachable(RuntimeError):
    """Service didn't answer at all (likely not running)."""


class DisplayClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -------- health / discovery -------------------------------------------

    async def health(self) -> dict:
        try:
            r = await self._client.get("/api/health", timeout=2.0)
        except httpx.RequestError as e:
            raise ServiceUnreachable(str(e)) from e
        r.raise_for_status()
        return r.json()

    async def fonts(self) -> list[dict]:
        r = await self._client.get("/api/fonts")
        r.raise_for_status()
        return r.json().get("fonts", [])

    # -------- send commands ------------------------------------------------

    async def _post_json(self, path: str, body: dict) -> Result:
        try:
            r = await self._client.post(path, json=body)
        except httpx.RequestError as e:
            raise ServiceUnreachable(str(e)) from e
        return _unpack(r)

    async def solid(self, color) -> Result:
        return await self._post_json("/api/solid", {"color": color})

    async def fade(self, color_a, color_b, frames: int = 40) -> Result:
        return await self._post_json(
            "/api/fade", {"color_a": color_a, "color_b": color_b, "frames": frames},
        )

    async def text_tiles(self, tile_pixels_b64: str, tile_count: int) -> Result:
        """Ship pre-rendered RGB332 tile bytes (canvas path — byte-identical to old ui.py)."""
        return await self._post_json(
            "/api/text/tiles",
            {"tile_pixels_b64": tile_pixels_b64, "tile_count": tile_count},
        )

    async def text(
        self,
        text: str,
        fg="#ffffff",
        bg="#000000",
        font: Optional[str] = "Helvetica",
        size: int = 17,
        letter_spacing: int = 0,
        y_offset: int = 0,
        antialias: bool = False,
    ) -> Result:
        return await self._post_json("/api/text", {
            "text": text, "fg": fg, "bg": bg, "font": font, "size": size,
            "letter_spacing": letter_spacing, "y_offset": y_offset,
            "antialias": antialias,
        })

    async def text_preview_png(
        self,
        text: str,
        fg="#ffffff",
        bg="#000000",
        font: Optional[str] = "Helvetica",
        size: int = 17,
        letter_spacing: int = 0,
        y_offset: int = 0,
        antialias: bool = False,
    ) -> tuple[bytes, int]:
        body = {
            "text": text, "fg": fg, "bg": bg, "font": font, "size": size,
            "letter_spacing": letter_spacing, "y_offset": y_offset,
            "antialias": antialias,
        }
        try:
            r = await self._client.post("/api/text/preview", json=body)
        except httpx.RequestError as e:
            raise ServiceUnreachable(str(e)) from e
        if r.status_code != 200:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise RuntimeError(f"preview failed ({r.status_code}): {msg}")
        return r.content, int(r.headers.get("X-Tile-Count", "1"))

    async def gif(self, gif_bytes: bytes, filename: str = "upload.gif") -> Result:
        try:
            r = await self._client.post(
                "/api/gif",
                files={"file": (filename, gif_bytes, "image/gif")},
            )
        except httpx.RequestError as e:
            raise ServiceUnreachable(str(e)) from e
        return _unpack(r)


def _unpack(r: httpx.Response) -> Result:
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "message": r.text[:200]}
    return Result(
        ok=bool(body.get("ok", r.is_success)),
        message=str(body.get("message", "")),
        status=r.status_code,
        meta=body.get("meta", {}) or {},
    )
