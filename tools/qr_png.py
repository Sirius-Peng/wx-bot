"""开发期校验工具：把二维码矩阵渲染成 PNG 供解码器核验。

⚠️ 仅用于开发验证，不参与运行时（运行时用 core/qr.py 的 SVG 输出）。
不依赖 PIL：直接手写最小 PNG（灰度 8bit）。
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.qr import encode_matrix  # noqa: E402


def _png_gray(pixels: list[list[int]], scale: int, border: int) -> bytes:
    n = len(pixels)
    side = (n + border * 2) * scale

    raw = bytearray()
    for y in range(side):
        raw.append(0)                       # filter type 0
        r = (y // scale) - border
        for x in range(side):
            c = (x // scale) - border
            if 0 <= r < n and 0 <= c < n:
                raw.append(0 if pixels[r][c] else 255)
            else:
                raw.append(255)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def save(text: str, out: Path, ec: str = "M", scale: int = 8, border: int = 4) -> None:
    m = encode_matrix(text, ec)
    out.write_bytes(_png_gray(m, scale, border))


if __name__ == "__main__":
    save(sys.argv[1] if len(sys.argv) > 1 else "HELLO",
         Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/qr.png"))
    print("saved")
