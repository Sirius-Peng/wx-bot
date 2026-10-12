"""开发期校验工具：独立解码器 + RS 校验，用来验证 `src/core/qr.py` 生成的二维码。

与 `src/core/qr.py` **尽量独立实现**：掩码函数、功能图案表、反交织、位流解析
都在本文件重写了一遍。只复用两处协议固定参数（重复实现只会引入不一致）：
格式信息 15 bit 的位置、RS 分块参数。

用法：
    python3 tools/qr_check.py          # 分块表核对 + 自解码 + RS 伴随式校验
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.qr import (  # noqa: E402
    FORMAT_POS_A, align_centers, format_pos_b,
)

_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"


def _gf() -> tuple[list[int], list[int]]:
    exp, log, x = [0] * 512, [0] * 256, 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf()


def _syndromes(msg: list[int], nsym: int) -> list[int]:
    """RS 伴随式：数据+纠错码字正确时全为 0。"""
    out = []
    for i in range(nsym):
        x = _GF_EXP[i]
        acc = 0
        for b in msg:
            acc = (0 if acc == 0 else _GF_EXP[_GF_LOG[acc] + _GF_LOG[x]]) ^ b
        out.append(acc)
    return out


def _mask(mask: int, r: int, c: int) -> bool:
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def _function_map(size: int) -> list[list[bool]]:
    """读取用的"非数据格"表 = 结构格 + 格式信息格 + 版本信息格。"""
    fn = [[False] * size for _ in range(size)]

    def fill(r0: int, c0: int, h: int, w: int) -> None:
        for r in range(r0, r0 + h):
            for c in range(c0, c0 + w):
                if 0 <= r < size and 0 <= c < size:
                    fn[r][c] = True

    fill(0, 0, 8, 8)
    fill(0, size - 8, 8, 8)
    fill(size - 8, 0, 8, 8)
    for i in range(size):
        fn[6][i] = True
        fn[i][6] = True
    for r, c in list(FORMAT_POS_A) + list(format_pos_b(size)):
        fn[r][c] = True
    fn[size - 8][8] = True

    version = (size - 17) // 4
    if version >= 2:
        centers = align_centers(version)
        for cy in centers:
            for cx in centers:
                if (cy < 9 and cx < 9) or (cy < 9 and cx > size - 10) \
                        or (cy > size - 10 and cx < 9):
                    continue
                fill(cy - 2, cx - 2, 5, 5)
    if version >= 7:
        fill(size - 11, 0, 3, 6)
        fill(0, size - 11, 6, 3)
    return fn


def decode(matrix: list[list[bool]]) -> str:
    """把矩阵解码回字符串（支持数字 / 字母数字 / 字节模式）。"""
    size = len(matrix)
    version = (size - 17) // 4
    if not 1 <= version <= 25:
        raise ValueError(f"不支持的尺寸：{size}")

    # 只读 A 组（两处副本内容相同）。
    # ⚠️ 不能把 A、B 两组位置拼起来一起 OR —— 那样得到的不是 15 位，
    # 而是两组各 15 位叠在一起，纠错级别/掩码全错。
    bits = 0
    for i, (r, c) in enumerate(FORMAT_POS_A):
        if matrix[r][c]:
            bits |= 1 << i
    # 解码：先把 15 位反转回"MSB 在前"的格式串，再异或固定掩码 0x5412，
    # 然后高 2 位是纠错级别、接着 3 位是掩码。
    fmt = int(format(bits, "015b")[::-1], 2) ^ 0b101010000010010
    ec = {1: "L", 0: "M", 3: "Q", 2: "H"}[(fmt >> 13) & 0b11]
    mask = (fmt >> 10) & 0b111

    fn = _function_map(size)
    stream_bits: list[int] = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            # 第 6 列是竖直定时图案，不能与它成对；实际成对的是 (5,4)。
            # 用 `col <= 6` 会让后续列号整体偏移，数据格少 8 个。
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for c in (col, col - 1):
                if c < 0 or fn[r][c]:
                    continue
                v = bool(matrix[r][c])
                if _mask(mask, r, c):
                    v = not v
                stream_bits.append(int(v))
        upward = not upward
        col -= 2

    # 末尾的 remainder bits（数据位数不是 8 的整数倍时才有）不属于任何码字
    from src.core.qr import total_codewords as _tc
    nbits = _tc(version, ec) * 8
    codewords = [int("".join(map(str, stream_bits[i:i + 8])), 2)
                 for i in range(0, nbits, 8)]

    from src.core.qr import _blocks_and_sizes
    blocks, sizes, ec_len = _blocks_and_sizes(version, ec)
    data_blocks: list[list[int]] = [[] for _ in range(blocks)]
    i = 0
    for k in range(max(sizes)):
        for b in range(blocks):
            if k < sizes[b]:
                data_blocks[b].append(codewords[i])
                i += 1
    ec_blocks: list[list[int]] = [[] for _ in range(blocks)]
    for _k in range(ec_len):
        for b in range(blocks):
            ec_blocks[b].append(codewords[i])
            i += 1
    for b in range(blocks):
        syn = _syndromes(data_blocks[b] + ec_blocks[b], ec_len)
        if any(syn):
            raise ValueError(f"第 {b} 块 RS 校验失败，伴随式={syn}")

    data = [cw for blk in data_blocks for cw in blk]
    stream = [(b >> k) & 1 for b in data for k in range(7, -1, -1)]

    pos = 0

    def take(n: int) -> int:
        nonlocal pos
        v = int("".join(map(str, stream[pos:pos + n])), 2)
        pos += n
        return v

    mode = take(4)
    if mode == 0b0100:
        length = take(8 if version <= 9 else 16)
        return bytes(take(8) for _ in range(length)).decode("utf-8")
    if mode == 0b0010:
        length = take(9 if version <= 9 else (11 if version <= 26 else 13))
        out = []
        while length >= 2:
            v = take(11)
            out.append(_ALNUM[v // 45] + _ALNUM[v % 45])
            length -= 2
        if length:
            out.append(_ALNUM[take(6)])
        return "".join(out)
    if mode == 0b0001:
        length = take(10 if version <= 9 else (12 if version <= 26 else 14))
        out = []
        while length >= 3:
            out.append(f"{take(10):03d}")
            length -= 3
        if length == 2:
            out.append(f"{take(7):02d}")
        elif length == 1:
            out.append(f"{take(4):01d}")
        return "".join(out)
    raise ValueError(f"不支持的模式：{mode:04b}")


if __name__ == "__main__":
    import src.core.qr as Q

    # ① 分块表必须与 QR 规范逐条一致（25 个版本 × 4 个纠错级别）。
    #    这一条需要 qrcode 包作为"规范数据源"，没装就跳过（不影响下面的自解码）。
    bad = 0
    try:
        import qrcode.base as _B
        from qrcode.constants import (ERROR_CORRECT_H, ERROR_CORRECT_L,
                                      ERROR_CORRECT_M, ERROR_CORRECT_Q)
        for v in range(1, 26):
            for name, e in (("L", ERROR_CORRECT_L), ("M", ERROR_CORRECT_M),
                            ("Q", ERROR_CORRECT_Q), ("H", ERROR_CORRECT_H)):
                ref = [x.data_count for x in _B.rs_blocks(v, e)]
                if Q._blocks_and_sizes(v, name)[1] != ref:
                    bad += 1
                    print(f"分块不一致 v={v}{name}: "
                          f"{Q._blocks_and_sizes(v, name)[1]} != {ref}")
        print(f"分块表 vs 规范（25 版本 × 4 级别）："
              f"{'全部一致' if bad == 0 else str(bad) + ' 处不一致'}")
    except ImportError:
        print("分块表 vs 规范：跳过（未安装 qrcode 包）")

    # ② 自解码 + 逐块 RS 伴随式校验（只用标准库）
    tests = [
        ("https://txz.qq.com/p?k=pkP3yWaDZXp0W3Pepkt4jHQgZHli8Csa&f=1600001615", "M"),
        ("HELLO WORLD", "M"), ("a", "M"),
        ("https://example.com/" + "x" * 40, "M"),
        ("中文测试内容：扫码登录 QQ 机器人", "M"),
        ("https://txz.qq.com/p?k=" + "A" * 60, "M"),
        ("0123456789" * 12, "L"), ("0123456789" * 12, "H"), ("x", "Q"),
        ("https://txz.qq.com/p?k=" + "B" * 120, "M"),
        ("https://txz.qq.com/p?k=" + "C" * 200, "M"), ("Z" * 300, "M"),
        ("中" * 100, "Q"),
    ]
    ok = 0
    total = len(tests) * 4
    for text, _ec in tests:
        for lv in ("L", "M", "Q", "H"):
            try:
                got = decode(Q.encode_matrix(text, lv))
            except Exception as e:
                print(f"FAIL ec={lv} {text[:30]!r}: {type(e).__name__}: {e}")
                continue
            if got == text:
                ok += 1
            else:
                print(f"MISMATCH ec={lv} {text[:30]!r} -> {got[:50]!r}")
    print(f"自解码 + 逐块 RS 校验通过：{ok}/{total}")
    sys.exit(0 if ok == total and bad == 0 else 1)
