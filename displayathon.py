"""Displayathon — BLE wire-protocol primitives for iledeyes-* LED signs.

This module is the thin protocol layer. It owns the BLE UUIDs, packet framing,
CRC, pixel quantization, and high-level handshake helpers. `display_service.py`
imports from here (as `dat`) and combines these primitives with FastAPI +
asyncio for the long-running HTTP service.

Protocol reverse-engineered via https://github.com/akkaisinabin/iledcolor-rs
(see that repo's docs/ouppy.md for the 0x54 protocol spec). Pixel format
(1-byte-per-pixel RGB332, RRRGGGBB) was determined by on-device testing — the
Rust code was for a dog collar which uses 3-byte RGB; this LED sign doesn't.

Target display: 96×16 RGB332.
"""
from __future__ import annotations

import asyncio
import io
import struct
from enum import IntEnum

from bleak import BleakClient

# ----------------------------------------------------------------------------
# device identity + GATT characteristics
# ----------------------------------------------------------------------------

NAME_PREFIX = "iledeyes"

SERVICE_UUID = "0000a950-0000-1000-8000-00805f9b34fb"
CMD_CHAR     = "0000a951-0000-1000-8000-00805f9b34fb"   # Connect/TestPass/StartStream/Brightness/LedEnable
WRITE_CHAR   = "0000a952-0000-1000-8000-00805f9b34fb"   # Continue chunks / EndStream
NOTIFY_CHAR  = "0000a953-0000-1000-8000-00805f9b34fb"

WIDTH = 96
HEIGHT = 16
CHUNK_SIZE = 492   # BLE MTU-3 budget (Rust reference value)


# ----------------------------------------------------------------------------
# packet framing
# ----------------------------------------------------------------------------

class Handle(IntEnum):
    Continue       = 0x00
    EndStream      = 0x01
    StartPlayList  = 0x03
    StartStream    = 0x06
    PlayCommit     = 0x08   # from longlog capture: sent after playlist items to start playback
    Brightness     = 0x09
    LedEnable      = 0x0A
    Connect        = 0x0D
    SetPass        = 0x0E
    TestPass       = 0x0F


def build_packet(
    handle: Handle,
    data: bytes,
    sequence: int | None = None,
    data_length: int | None = None,
    data_chksum: bool = False,
) -> bytes:
    """0x54 | handle | len_BE | [seq_BE_u32]? | [data_len_BE_u16]? | [data_chksum_BE_u16]? | data | ck_BE_u16

    `data_chksum=True` inserts a 2-byte BE sum-of-data-bytes checksum (per
    CrcUtils.getShortCheckNum in the official app). Required for Continue
    packets to make the device accept larger animations.
    """
    body = b""
    if sequence is not None:
        body += struct.pack(">I", sequence)
    if data_length is not None:
        body += struct.pack(">H", data_length)
    if data_chksum:
        body += struct.pack(">H", sum(data) & 0xFFFF)
    body += data
    length = len(body) + 2
    packet = bytes([0x54, handle.value]) + struct.pack(">H", length) + body
    return packet + struct.pack(">H", sum(packet) & 0xFFFF)


# ----------------------------------------------------------------------------
# CRC-32C (Castagnoli) — used for CtnData wrapping
# ----------------------------------------------------------------------------

_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE = [0] * 256
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = (_crc >> 1) ^ (_CRC32C_POLY if _crc & 1 else 0)
    _CRC32C_TABLE[_i] = _crc


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


# ----------------------------------------------------------------------------
# pixel encoding
# ----------------------------------------------------------------------------

def rgb_to_rgb332(r: int, g: int, b: int) -> int:
    """Pack 8-8-8 RGB to 3-3-2 RGB in one byte: RRRGGGBB."""
    return (r & 0xE0) | ((g & 0xE0) >> 3) | ((b & 0xC0) >> 6)


# ----------------------------------------------------------------------------
# 22-byte content headers
# ----------------------------------------------------------------------------

def _metadata(width: int, height: int, frame_count: int = 1, frame_ms: int = 50) -> bytes:
    """22-byte image header per ouppy.md spec.

    Layout (big-endian, byte offsets relative to the CtnData payload start):
      0-3   : unk1, unk2  (u16+u16, both 0)
      4-7   : width, height (u16+u16)
      8-9   : unk3 = 0 (u16)
      10-11 : unk4 = 0x0001  (mode = RGB332)
      12-13 : unk5 = frame_count (cycles N frames natively)
      14-15 : unk6 = 0x0001
      16    : per-frame ms (u8)
      17    : 0x00
      18    : 0x64 (= 100)
      19-21 : 3 zero bytes
    """
    return struct.pack(
        ">HHHHHHHH",
        0x0000, 0x0000,
        width, height,
        0x0000,
        0x0001,        # unk4: mode = RGB332
        frame_count,   # unk5
        0x0001,        # unk6
    ) + bytes([
        frame_ms & 0xFF,
        0x00,
        0x64,
        0x00, 0x00, 0x00,
    ])


def _metadata_text_scroll(width: int, height: int, frame_count: int) -> bytes:
    """22-byte header that asks the device to scroll a text bitmap natively.

    The app splits rendered text into consecutive 96-pixel-wide screenfuls and
    sends them as N "frames" of WIDTH×HEIGHT each; the device plays them as a
    scrolling strip. `unk4=0x0000, unk6=0x0101, byte@16=0x00` is the
    scroll-mode signal (matches TextBean effect=1 LTR).
    """
    return struct.pack(
        ">HHHHHHHH",
        0x0000, 0x0000,
        width, height,
        0x0000,
        0x0000,        # unk4 — was 0x0001 for RGB332 static
        frame_count,   # unk5 = how many 96-wide tiles of text
        0x0101,        # unk6 — high-byte 0x01 = scroll LTR
    ) + bytes([
        0x00, 0x00, 0x64, 0x00, 0x00, 0x00,
    ])


def _metadata_gif_native(width: int, height: int) -> bytes:
    """22-byte header that tells the device to decode a raw .gif file payload.

    Captured from the iOS app sending a playlist of two GIFs: every chunk's
    payload was CtnData(24) + this 22-byte header + the *raw* .gif file bytes.
    `unk4=0x0006` flips the firmware into native-GIF mode; `b16=0x04` (vs
    0x32 for static RGB332 and 0x00 for scroll text) is distinctive of this
    mode.
    """
    return struct.pack(
        ">HHHHHHHH",
        0x0000, 0x0000,
        width, height,
        0x0000,
        0x0006,   # unk4 — NATIVE GIF mode
        0x0001,   # unk5 — single resource
        0x0001,   # unk6 — plain (not 0x0101 scroll flag)
    ) + bytes([
        0x04, 0x00, 0x64, 0x00, 0x00, 0x00,
    ])


# ----------------------------------------------------------------------------
# CtnData wrap + start/commit packets
# ----------------------------------------------------------------------------

def wrap_ctn(payload: bytes) -> tuple[bytes, int, int]:
    """CtnData: crc32c(4) | 0x01 | 19×0x00 | payload."""
    crc = crc32c(payload)
    wrapped = struct.pack(">I", crc) + b"\x01" + b"\x00" * 19 + payload
    return wrapped, crc, len(wrapped)


def build_start_stream(crc: int, total_len: int) -> bytes:
    """StaData: crc32(4 BE) | total_len(4 BE) | 0x000000 (3 pad)."""
    data = struct.pack(">I", crc) + struct.pack(">I", total_len) + b"\x00\x00\x00"
    return build_packet(Handle.StartStream, data)


def build_start_playlist(count: int, idx: int, crc: int, total_len: int) -> bytes:
    """StartPlayList variant used for native-GIF sends.

    Capture shape:
        54 03 00 10  count(1) idx(1) crc32(4BE) total_len(4BE) 01 00 00 00  ck(2)
    """
    data = (
        bytes([count & 0xFF, idx & 0xFF]) +
        struct.pack(">I", crc) +
        struct.pack(">I", total_len) +
        b"\x01\x00\x00\x00"
    )
    return build_packet(Handle.StartPlayList, data)


def build_play_commit() -> bytes:
    """Final "start playing the playlist we just uploaded" command.

    From longlog: `54 08 00 03 01 00 60`.
    """
    return build_packet(Handle.PlayCommit, b"\x01")


# ----------------------------------------------------------------------------
# high-level BLE handshake helpers
# ----------------------------------------------------------------------------

async def connect_and_authenticate(client: BleakClient) -> None:
    await client.write_gatt_char(CMD_CHAR, build_packet(Handle.Connect, b"\x00"), response=False)
    await asyncio.sleep(0.05)
    await client.write_gatt_char(CMD_CHAR, build_packet(Handle.TestPass, b"\x00" * 6), response=False)
    await asyncio.sleep(0.05)


async def set_brightness(client: BleakClient, level: int) -> None:
    """0–1 brightest, 10 dimmest."""
    if not 0 <= level <= 10:
        raise ValueError("level 0..10")
    data = bytes([level]) + b"\x00" * 8
    await client.write_gatt_char(CMD_CHAR, build_packet(Handle.Brightness, data), response=False)


async def set_enabled(client: BleakClient, on: bool) -> None:
    data = bytes([0x01 if on else 0x00]) + b"\x00" * 8
    await client.write_gatt_char(CMD_CHAR, build_packet(Handle.LedEnable, data), response=False)


# ----------------------------------------------------------------------------
# GIF resize helper (used by /api/gif when uploaded GIFs aren't already 96×16)
# ----------------------------------------------------------------------------

def _resize_gif_to_display(src_path: str) -> bytes:
    """Open a GIF, resize every frame to WIDTH×HEIGHT, re-encode as GIF.
    Returns the new .gif bytes. Preserves per-frame durations.
    """
    from PIL import Image, ImageSequence
    src = Image.open(src_path)
    frames = []
    durations = []
    for frame in ImageSequence.Iterator(src):
        rgb = frame.convert("RGB").resize((WIDTH, HEIGHT))
        frames.append(rgb)
        durations.append(frame.info.get("duration", 100))
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0, disposal=2,
    )
    return buf.getvalue()
