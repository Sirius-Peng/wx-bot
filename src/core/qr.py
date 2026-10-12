"""二维码生成（纯 Python，零依赖）。

═══════════════════════════════════════════════════════════════════
为什么要自己写一个？

NapCat 的登录接口只返回一个**二维码内容 URL**（`https://txz.qq.com/p?k=...`），
需要我们自己把它转成二维码。而项目有一条硬约束：

    **零外部依赖** —— 面向非技术用户，装依赖越少越好；
    桌面版还要用 PyInstaller 打包，多一个依赖就多几十 MB 与一类失败可能。

二维码编码本质上只是一段确定的数学（位流 + RS 纠错 + 掩码评分），
用标准库完全能实现。输出 **SVG**（矢量，任意尺寸都清晰、前端可直接当 data URL 用），
不需要图像库。

═══════════════════════════════════════════════════════════════════
⚠️ 这个文件踩过的坑（都很隐蔽，改代码前先读一遍）

1. **格式信息位的位置不是镜像对称的**。第一版把低位写在上边一竖、
   高位写在下边一行，结果矩阵看着完全正常，但任何扫码器都读不出格式信息
   → 判定为"没有二维码"。见 `FORMAT_POS_A` / `format_pos_b`。

2. **对齐图案坐标表抄错了一行**（把版本 2 的坐标写进版本 1 的位置），
   v2 以上全部画错位置。现在改为按公式现算，不再抄表。

3. **分块表存的是"每块数据码字"还是"总数据码字"**，第一版搞混了，
   v2 以上 M/Q/H 级别的数据区偏大。唯一可靠的验收标准是
   **数据格总数 == 总码字数 × 8**。

4. **定时图案范围**是 8 .. size-9（两端各被"7 格定位图案 + 1 格分隔符"占掉），
   差一格就让整个数据区错位。

5. **固定暗模块**在数据填充、掩码之后才写，否则会被掩码翻掉。

验收方式：`python3 tools/qr_check.py`
（独立实现的解码器 + RS 伴随式校验 + 与系统 CoreImage 的逐格比对）。
"""
from __future__ import annotations

# ── 常量 ──────────────────────────────────────────────────────────

MODE_BYTE = 0b0100

#: 纠错级别 → 格式信息里的 2 bit 编码
EC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

#: 纠错级别 → 表内下标（表按 L,M,Q,H 排列）
EC_INDEX = {"L": 0, "M": 1, "Q": 2, "H": 3}



#: RS 分块参数：`"v<版本><级别>"` → (块数, 每块数据码字**基数**, 每块纠错码字数)
#:
#: · 每块数据码字 = 基数 或 基数 + 1（规范里块大小最多相差 1，
#:   如 v14-L = 115/115/115/116），多出的块数由数据模块总数反推。
#: · 表里存的是**每块**的基数，不是总数据码字数 ——
#:   第一版搞混了这两者，v2 以上、M/Q/H 级别的数据区全部偏大，
#:   生成出来的二维码图案看着正常，扫码器却完全读不出来。
#: · 验收标准只有一条：**数据格总数 == 总码字数 × 8**，
#:   `tools/qr_check.py` 对 25 个版本 × 4 个级别全部做了断言。
_BLOCK_TABLE: dict[str, tuple[int, int, int]] = {
    "v1L": (1, 19, 7), "v1M": (1, 16, 10), "v1Q": (1, 13, 13), "v1H": (1, 9, 17),
    "v2L": (1, 34, 10), "v2M": (1, 28, 16), "v2Q": (1, 22, 22), "v2H": (1, 16, 28),
    "v3L": (1, 55, 15), "v3M": (1, 44, 26), "v3Q": (2, 17, 18), "v3H": (2, 13, 22),
    "v4L": (1, 80, 20), "v4M": (2, 32, 18), "v4Q": (2, 24, 26), "v4H": (4, 9, 16),
    "v5L": (1, 108, 26), "v5M": (2, 43, 24), "v5Q": (4, 15, 18), "v5H": (4, 11, 22),
    "v6L": (2, 68, 18), "v6M": (4, 27, 16), "v6Q": (4, 19, 24), "v6H": (4, 15, 28),
    "v7L": (2, 78, 20), "v7M": (4, 31, 18), "v7Q": (6, 14, 18), "v7H": (5, 13, 26),
    "v8L": (2, 97, 24), "v8M": (4, 38, 22), "v8Q": (6, 18, 22), "v8H": (6, 14, 26),
    "v9L": (2, 116, 30), "v9M": (5, 36, 22), "v9Q": (8, 16, 20), "v9H": (8, 12, 24),
    "v10L": (4, 68, 18), "v10M": (5, 43, 26), "v10Q": (8, 19, 24), "v10H": (8, 15, 28),
    "v11L": (4, 81, 20), "v11M": (5, 50, 30), "v11Q": (8, 22, 28), "v11H": (11, 12, 24),
    "v12L": (4, 92, 24), "v12M": (8, 36, 22), "v12Q": (10, 20, 26), "v12H": (11, 14, 28),
    "v13L": (4, 107, 26), "v13M": (9, 37, 22), "v13Q": (12, 20, 24), "v13H": (16, 11, 22),
    "v14L": (4, 115, 30), "v14M": (9, 40, 24), "v14Q": (16, 16, 20), "v14H": (16, 12, 24),
    "v15L": (6, 87, 22), "v15M": (10, 41, 24), "v15Q": (12, 24, 30), "v15H": (18, 12, 24),
    "v16L": (6, 98, 24), "v16M": (10, 45, 28), "v16Q": (17, 19, 24), "v16H": (16, 15, 30),
    "v17L": (6, 107, 28), "v17M": (11, 46, 28), "v17Q": (16, 22, 28), "v17H": (19, 14, 28),
    "v18L": (6, 120, 30), "v18M": (13, 43, 26), "v18Q": (18, 22, 28), "v18H": (21, 14, 28),
    "v19L": (7, 113, 28), "v19M": (14, 44, 26), "v19Q": (21, 21, 26), "v19H": (25, 13, 26),
    "v20L": (8, 107, 28), "v20M": (16, 41, 26), "v20Q": (20, 24, 30), "v20H": (25, 15, 28),
    "v21L": (8, 116, 28), "v21M": (17, 42, 26), "v21Q": (23, 22, 28), "v21H": (25, 16, 30),
    "v22L": (9, 111, 28), "v22M": (17, 46, 28), "v22Q": (23, 24, 30), "v22H": (34, 13, 24),
    "v23L": (9, 121, 30), "v23M": (18, 47, 28), "v23Q": (25, 24, 30), "v23H": (30, 15, 30),
    "v24L": (10, 117, 30), "v24M": (20, 45, 28), "v24Q": (27, 24, 30), "v24H": (32, 16, 30),
    "v25L": (12, 106, 26), "v25M": (21, 47, 28), "v25Q": (29, 24, 30), "v25H": (35, 15, 30),
}

#: 15 bit 格式信息的模块位置（**唯一真相来源**）。
#:
#: 写入（`_Matrix.write_format`）与占位（`_Matrix._reserve_format_area`）
#: 都用它，避免"图案画对了、格式信息写错位"这种隐蔽 bug。
#:
#: A 组在左上角定位图案周围：低 6 位竖排、位 6/7/8 在角上、高 6 位横排。
#: A 组格式信息位置（15 格，bit i 放第 i 个）。
#:
#: 注意 `(8, 6)` 与 `(6, 8)` 被跳过 —— 那是定时图案。
FORMAT_POS_A: tuple[tuple[int, int], ...] = (
    (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5),   # bit 0..5
    (8, 7),                                            # bit 6
    (8, 8),                                            # bit 7
    (7, 8),                                            # bit 8
    (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),    # bit 9..14
)


def format_pos_b(size: int) -> tuple[tuple[int, int], ...]:
    """B 组格式信息位置（15 格，bit i 放第 i 个）。

    · bit 0..6   →  列 8，行 `size-1` .. `size-7`（从下往上）
    · bit 7      →  **(8, size-8)** —— 与固定暗模块 (size-8, 8) 不是同一格！
    · bit 8..14  →  行 8，列 `size-7` .. `size-1`（从左往右）

    这组位置是从参考实现（Project Nayuki qrcodegen）的写入日志里逐条核对出来的。
    两个最容易搞错的点：
      1. bit 7 在 (8, size-8)，**不在** (size-8, 8)；
      2. 固定暗模块在 **(size-8, 8)**（左下角定位图案右侧），不在 (8, size-8)。
    两者差一格，写反了格式信息就整体错位，任何扫码器都判定"没有二维码"。
    """
    return tuple(
        [(size - 1 - i, 8) for i in range(7)]        # bit 0..6
        + [(8, size - 8)]                            # bit 7
        + [(8, size - 7 + i) for i in range(7)]      # bit 8..14
    )



class QRDataTooLong(ValueError):
    """内容超出当前支持的最大容量。"""


# ── 矩阵结构 ──────────────────────────────────────────────────────

def align_centers(version: int) -> list[int]:
    """对齐图案中心坐标（版本 1 没有）。

    公式取自 Project Nayuki 的参考实现：
        step = (version*8 + numalign*3 + 5) // (numalign*4 - 4) * 2
        centers = [size-7-i*step for i in range(numalign-1)] + [6]

    ⚠️ 这里刻意**现算**而不是抄表。曾经抄过一张对齐坐标表（整体错位一行），
    也用过另一个看似等价、但对 v19 会算出 34 而非 30 的公式 ——
    两者都会让 v2 以上部分版本的二维码彻底扫不出来。
    """
    if version <= 1:
        return []
    numalign = version // 7 + 2
    size = version * 4 + 17
    step = (version * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
    return [6] + [(size - 7 - i * step) for i in range(numalign - 2, -1, -1)]


def _format_bits(ec: str, mask: int) -> int:
    """15 bit 格式信息。

    ⚠️ 这里返回的是**位序反转后的整数**，因为本文件的位置表
    （`FORMAT_POS_A` / `format_pos_b`）是按"第 i 格放 bit i（LSB 起）"定义的，
    而 BCH + 掩码算出来的 15 bit 是 MSB 在前的。
    不反转也能印出一张"看着正常"的二维码，但任何扫码器都读不出格式信息，
    直接判定为"没有二维码"（Vision / zbar / 微信全部扫不到）。
    交叉验证：`_format_bits('L',0)` 必须等于 `0b111011111000100`。
    """
    data = (EC_BITS[ec] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412
    out = 0
    for i in range(15):
        if (bits >> i) & 1:
            out |= 1 << (14 - i)
    return out


def _version_bits(version: int) -> int:
    """18 bit 版本信息（版本 ≥ 7 才需要）。"""
    return (version << 12) | _bch_remainder(version << 12, 0b1111100100101)


def _bch_remainder(data: int, generator: int) -> int:
    gen_bits = generator.bit_length()
    rem = data
    while rem.bit_length() >= gen_bits:
        rem ^= generator << (rem.bit_length() - gen_bits)
    return rem


class _Matrix:
    """二维码矩阵。

    `function[r][c] = True` 表示该格是**功能格**，不参与数据填充
    （定位 / 分隔符 / 定时 / 对齐 / 格式信息 / 版本信息 / 固定暗模块）。
    """

    def __init__(self, version: int):
        self.version = version
        self.size = version * 4 + 17
        self.modules: list[list[bool | None]] = [
            [None] * self.size for _ in range(self.size)
        ]
        self.function: list[list[bool]] = [
            [False] * self.size for _ in range(self.size)
        ]

    # ── 基础写入 ──

    def set(self, row: int, col: int, value: bool,
            is_function: bool = True) -> None:
        """写一个模块。

        ⚠️ 越界直接报错，**不静默忽略**：早期版本用 `if 0 <= row < size`
        静默丢弃，参数名被循环变量覆盖时图案画到网格外也不报错，
        二维码整体错位却查不出原因。
        """
        if not (0 <= row < self.size and 0 <= col < self.size):
            raise IndexError(f"模块坐标越界：({row}, {col}) size={self.size}")
        self.modules[row][col] = value
        if is_function:
            self.function[row][col] = True

    # ── 功能图案 ──

    def _finder(self, top: int, left: int) -> None:
        """9×9 定位图案区域（7×7 图案 + 一圈分隔符），中心在 (top+3, left+3)。

        采用"中心 + 切比雪夫距离"的写法（与参考实现一致）：
        `max(|dx|,|dy|) not in (2, 4)` —— 距离 2 是白色内圈、距离 4 是分隔符，
        其余为黑。这样一次循环就把图案和分隔符都画全，
        包括被定时图案穿过的那两格（(6,7)、(7,6) 必须被压成浅色）。

        ⚠️ 早期版本按"7×7 图案 + 单独画分隔符"写，漏掉了
        (0,8)、(8,0) 等分隔符格，数据区莫名多出 4 格。
        """
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                row, col = top + 3 + dy, left + 3 + dx
                if 0 <= row < self.size and 0 <= col < self.size:
                    self.set(row, col, max(abs(dx), abs(dy)) not in (2, 4))

    def _alignment(self, cx: int, cy: int) -> None:
        """5×5 对齐图案。"""
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                self.set(cy + dr, cx + dc, max(abs(dr), abs(dc)) != 1)

    def _timing(self) -> None:
        """定时图案：第 6 行与第 6 列**整条**交替排列。

        先铺满全长、再由定位图案覆盖两端（`_finder` 在它之后调用），
        这样与参考实现完全一致。分隔符所在的 (6,7)、(7,6) 会被定位图案压成浅色。
        """
        for i in range(self.size):
            dark = (i % 2 == 0)
            self.set(6, i, dark)
            self.set(i, 6, dark)

    def _reserve_format_area(self) -> None:
        """占位格式信息区（30 格）+ 固定暗模块，并把整块标为功能格。

        范围（以 v1 为例，`size=21`）：
          · 行 8：列 0..8、13..20      · 列 8：行 0..8、13..20
          两组位置表合起来正好 30 格（`FORMAT_POS_A` + `format_pos_b`），
          另加固定暗模块 (8, size-8) 共 31 格。

        加上定位图案的 192 格与定时图案的 10 格，总数正好等于
        `总格数 − 数据位数`（v1：441 − 208 = 233）。
        这个恒等式就是本文件最可靠的验收标准 ——
        一旦位置表算错，`apply_mask()` 的数量断言会立刻报错，
        而不是生成一张"看着正常、扫码器读不出"的坏码。
        """
        for row, col in list(FORMAT_POS_A) + list(format_pos_b(self.size)):
            if self.modules[row][col] is None:
                self.set(row, col, False)
            else:
                self.function[row][col] = True
        # 固定暗模块：**(size-8, 8)** —— 左下角定位图案右侧那一格，恒为深色。
        # ⚠️ 不是 (8, size-8)：后者是 B 组 bit 7，写错位置格式信息就整体错位。
        self.set(self.size - 8, 8, True)

    def _reserve_version(self) -> None:
        """版本信息区（版本 ≥ 7 才有），同样不参与数据填充。"""
        if self.version < 7:
            return
        for i in range(6):
            for j in range(3):
                self.set(self.size - 11 + j, i, False)
                self.set(i, self.size - 11 + j, False)

    def build_function_patterns(self) -> None:
        size = self.size
        self._timing()                      # 先铺定时图案
        self._finder(0, 0)                  # 定位图案覆盖定时图案两端
        self._finder(0, size - 7)
        self._finder(size - 7, 0)

        centers = align_centers(self.version)
        for cy in centers:
            for cx in centers:
                # 与三个定位图案重叠的位置不放（规范如此）
                if (cy < 9 and cx < 9) or (cy < 9 and cx > size - 10) \
                        or (cy > size - 10 and cx < 9):
                    continue
                self._alignment(cx, cy)

        self._reserve_format_area()   # 含 30 个格式位 + 固定暗模块 (8, size-8)
        self._reserve_version()

    # ── 数据 ──

    def data_cells(self) -> list[tuple[int, int]]:
        """数据格坐标，**按规范规定的读取顺序**（Z 字形蛇形）。

        这个顺序同时用于写入和读取，是唯一真相来源。
        """
        size = self.size
        out: list[tuple[int, int]] = []
        col = size - 1
        upward = True
        while col > 0:
            if col == 6:
                # 第 6 列是竖直定时图案：不能与它组成一对，
                # 实际成对的是 (5, 4)。这里减 1 之后再由末尾的 `col -= 2`
                # 变成 3，从而得到 (5,4) —— 早期写成 `col -= 1` 后直接配对，
                # 会得到 (4,3) 并跳过整列，数据格少 8 个、全部错位。
                col -= 1
            for row in (range(size - 1, -1, -1) if upward else range(size)):
                for c in (col, col - 1):
                    if c < 0 or self.function[row][c]:
                        continue
                    out.append((row, c))
            upward = not upward
            col -= 2
        return out

    def place_bits(self, bits: list[int]) -> None:
        """按数据格顺序写入码字位。"""
        self.apply_mask(0, bits)

    # ── 掩码 ──

    @staticmethod
    def mask_fn(mask: int, r: int, c: int) -> bool:
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

    def apply_mask(self, mask: int, bits: list[int]) -> None:
        """把数据格重算为"原始位 XOR 掩码"。

        每次都从原始位重算，所以不需要撤销上一次掩码。
        ⚠️ 位数量必须与数据格数量**完全相等**，否则说明格式区/版本区占位算错了
        （这个断言让"数据区大小对不上"这类隐蔽 bug 立刻暴露，而不是生成一张
        看着正常却扫不出来的二维码）。
        """
        cells = self.data_cells()
        # 数据格数 = 码字总位数 + remainder bits（0~7 位，规范要求补 0）。
        # 少一格多一格都说明结构算错了，直接报错而不是生成坏码。
        if len(cells) < len(bits) or len(cells) - len(bits) > 7:
            raise ValueError(
                f"数据格 {len(cells)} 格与码字位 {len(bits)} 位不匹配"
                f"（版本 {self.version}，remainder 应为 0~7）"
            )
        fn = self.mask_fn
        n = len(bits)
        for i, (row, col) in enumerate(cells):
            bit = bool(bits[i]) if i < n else False      # remainder bits 补 0
            self.modules[row][col] = (not bit) if fn(mask, row, col) else bit

    def write_format(self, ec: str, mask: int) -> None:
        """写入 15 bit 格式信息（两处副本）+ 固定暗模块。"""
        bits = _format_bits(ec, mask)
        for i, (row, col) in enumerate(FORMAT_POS_A):
            self.modules[row][col] = bool((bits >> i) & 1)
        for i, (row, col) in enumerate(format_pos_b(self.size)):
            self.modules[row][col] = bool((bits >> i) & 1)
        # 固定暗模块必须最后写（掩码会翻转它）
        self.modules[self.size - 8][8] = True

        if self.version >= 7:
            vbits = _version_bits(self.version)
            for i in range(18):
                b = bool((vbits >> i) & 1)
                self.modules[self.size - 11 + i % 3][i // 3] = b
                self.modules[i // 3][self.size - 11 + i % 3] = b

    # ── 掩码评分 ──

    def penalty(self) -> int:
        """规范里的四个惩罚项，总分越低越好（选掩码用）。"""
        size = self.size
        rows = [[bool(x) for x in row] for row in self.modules]
        cols = [[rows[r][c] for r in range(size)] for c in range(size)]
        score = 0

        for line in rows + cols:
            run, prev = 1, line[0]
            for v in line[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    prev, run = v, 1
            if run >= 5:
                score += 3 + (run - 5)

        for r in range(size - 1):
            a, b = rows[r], rows[r + 1]
            for c in range(size - 1):
                v = a[c]
                if v == a[c + 1] == b[c] == b[c + 1]:
                    score += 3

        # 疑似定位图案：用位掩码在整数上匹配，比逐格切片快得多
        pats = (0b10111010000, 0b00001011101)
        for line in rows + cols:
            n = 0
            for v in line:
                n = (n << 1) | int(v)
            for shift in range(size - 10):
                if ((n >> shift) & 0b11111111111) in pats:
                    score += 40

        dark = sum(sum(row) for row in rows)
        score += abs(dark * 100 // (size * size) - 50) // 5 * 10
        return score


# ── GF(256) 与 Reed-Solomon ───────────────────────────────────────

def _gf_tables() -> tuple[list[int], list[int]]:
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


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """生成多项式 g(x) = ∏ (x - α^i)。"""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _GF_EXP[i])
        poly = nxt
    return poly


def _rs_encode(data: list[int], ec_len: int,
               gen_cache: dict[int, list[int]]) -> list[int]:
    gen = gen_cache.get(ec_len)
    if gen is None:
        gen = _rs_generator(ec_len)
        gen_cache[ec_len] = gen
    res = [0] * ec_len
    for byte in data:
        factor = byte ^ res[0]
        res = res[1:] + [0]
        if factor:
            for i, g in enumerate(gen[1:]):
                res[i] ^= _gf_mul(g, factor)
    return res


# ── 容量与分块 ────────────────────────────────────────────────────

MAX_VERSION = 25


def data_bit_count(version: int) -> int:
    """数据位总数（**含 remainder bits**），按规范公式直接算。

    公式出自 ISO/IEC 18004，与 Project Nayuki 的参考实现一致：
        (16v + 128)v + 64
        v ≥ 2 再减 (25·n − 10)·n − 55，其中 n = v//7 + 2（对齐图案格数）
        v ≥ 7 再减 36（版本信息区）

    ⚠️ 为什么不用矩阵里"非功能格"的数量？
    两者**不相等**：格式信息占位区在数据填充阶段其实是可写的，
    填完才被格式位覆盖，所以"非功能格"比数据位多几个零头。
    直接数矩阵会得到 359（v2），比规范的 352 多 7 —— 那是 remainder bits，
    **不参与纠错编码**，只在填充末尾补 0。
    第一版正是拿矩阵格数当数据位用，导致 v2 以上数据区偏大、二维码扫不出。
    """
    if not 1 <= version <= MAX_VERSION:
        raise ValueError(f"版本超出范围：{version}")
    result = (16 * version + 128) * version + 64
    if version >= 2:
        numalign = version // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if version >= 7:
            result -= 36
    return result


def data_codewords(version: int, ec: str) -> int:
    """数据码字总数 = 总码字数 − 纠错码字总数。

    只依赖规范公式 + 表里的块数/纠错数，**不调用 _blocks_and_sizes**，
    避免两者互相递归（曾经写成互相调用，直接爆栈）。
    """
    blocks, _base, ec_len = _BLOCK_TABLE[f"v{version}{ec}"]
    return data_bit_count(version) // 8 - blocks * ec_len


def total_codewords(version: int, ec: str) -> int:
    """数据码字 + 纠错码字的总数（= 数据位总数 ÷ 8）。"""
    blocks, _base, ec_len = _BLOCK_TABLE[f"v{version}{ec}"]
    return data_codewords(version, ec) + blocks * ec_len


def _blocks_and_sizes(version: int, ec: str) -> tuple[int, list[int], int]:
    """返回 (块数, 每块数据码字数列表, 每块纠错码字数)。

    规范里块大小最多相差 1：`num_long` 个块各多 1 个数据码字
    （如 v14-L = 115/115/115/116）。`num_long` 由数据码字总数反推，
    这样"表"和"规范公式"永远不会互相矛盾 ——
    而这两者对不上正是本文件历史上最难查的 bug 来源。
    """
    blocks, base, ec_len = _BLOCK_TABLE[f"v{version}{ec}"]
    total_data = data_codewords(version, ec)
    num_long = total_data - blocks * base
    if not 0 <= num_long <= blocks:
        raise ValueError(
            f"分块表与规范公式对不上：v{version}-{ec} "
            f"表={blocks}块×{base}数据+{ec_len}纠错，公式算出 {total_data} 个数据码字"
        )
    # 规范里**短块在前、长块在后**（写反了数据分块就错，二维码扫不出）
    return blocks, [base] * (blocks - num_long) + [base + 1] * num_long, ec_len


def _char_count_bits(version: int) -> int:
    return 8 if version <= 9 else 16


def _pick_version(nbytes: int, ec: str) -> int:
    """选能装下 nbytes 个字节的最小版本。"""
    for v in range(1, MAX_VERSION + 1):
        if 4 + _char_count_bits(v) + nbytes * 8 <= data_codewords(v, ec) * 8:
            return v
    raise QRDataTooLong(f"内容太长（{nbytes} 字节），超出二维码容量（版本上限 {MAX_VERSION}）")


class _BitBuffer(list):
    def put(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.append((value >> i) & 1)

    def put_bytes(self, data: bytes) -> None:
        for b in data:
            self.put(b, 8)


def _build_codewords(payload: bytes, version: int, ec: str) -> list[int]:
    """位流 → 分块 → RS 纠错 → 交织，得到最终码字序列。"""
    total_data = data_codewords(version, ec)

    bb = _BitBuffer()
    bb.put(MODE_BYTE, 4)
    bb.put(len(payload), _char_count_bits(version))
    bb.put_bytes(payload)

    capacity = total_data * 8
    if len(bb) > capacity:
        raise QRDataTooLong("内容超出版本容量")
    bb.put(0, min(4, capacity - len(bb)))          # 结束符（最多 4 bit）
    if len(bb) % 8:
        bb.put(0, 8 - len(bb) % 8)                 # 补齐到字节边界

    codewords = [int("".join(str(b) for b in bb[i:i + 8]), 2)
                 for i in range(0, len(bb), 8)]
    pads = (0xEC, 0x11)                            # 交替填充直至填满
    i = 0
    while len(codewords) < total_data:
        codewords.append(pads[i % 2])
        i += 1

    # 分块 + 纠错
    blocks, sizes, ec_len = _blocks_and_sizes(version, ec)
    if sum(sizes) != total_data:
        raise ValueError(
            f"分块表与数据区不一致：v{version}-{ec} 表内 {sum(sizes)} != 实际 {total_data}"
        )

    cache: dict[int, list[int]] = {}
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    pos = 0
    for size in sizes:
        chunk = codewords[pos:pos + size]
        pos += size
        data_blocks.append(chunk)
        ec_blocks.append(_rs_encode(chunk, ec_len, cache))

    # 交织：先按列取数据码字，再按列取纠错码字
    out: list[int] = []
    for i in range(max(sizes)):
        for blk in data_blocks:
            if i < len(blk):
                out.append(blk[i])
    for i in range(ec_len):
        for blk in ec_blocks:
            out.append(blk[i])
    return out


# ── 对外接口 ──────────────────────────────────────────────────────

def encode_matrix(text: str, ec: str = "M") -> list[list[bool]]:
    """把文本编码为二维码矩阵（True = 黑）。"""
    if ec not in EC_BITS:
        raise ValueError(f"不支持的纠错级别：{ec}")
    payload = text.encode("utf-8")
    if not payload:
        raise ValueError("内容不能为空")

    version = _pick_version(len(payload), ec)
    codewords = _build_codewords(payload, version, ec)
    bits = [(b >> i) & 1 for b in codewords for i in range(7, -1, -1)]

    m = _Matrix(version)
    m.build_function_patterns()

    # ⚠️ bits 只有「总码字 × 8」位，比数据格少几个（remainder bits）。
    # apply_mask 会把剩余格当 0 处理并一起参与掩码 —— 与参考实现一致。
    best_mask, best_penalty = 0, None
    for mask in range(8):
        m.apply_mask(mask, bits)
        m.write_format(ec, mask)
        p = m.penalty()
        if best_penalty is None or p < best_penalty:
            best_mask, best_penalty = mask, p

    m.apply_mask(best_mask, bits)
    m.write_format(ec, best_mask)
    return [[bool(v) for v in row] for row in m.modules]


def to_svg(matrix: list[list[bool]], scale: int = 8, border: int = 4,
           dark: str = "#000000", light: str = "#ffffff") -> str:
    """矩阵 → SVG 字符串（可直接当 data URL 用）。"""
    n = len(matrix)
    side = (n + border * 2) * scale
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}" '
        f'viewBox="0 0 {side} {side}" shape-rendering="crispEdges">',
        f'<rect width="{side}" height="{side}" fill="{light}"/>',
        f'<g fill="{dark}">',
    ]
    for r, row in enumerate(matrix):           # 逐行合并连续黑块，减少节点数
        c = 0
        while c < n:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < n and row[c]:
                c += 1
            parts.append(
                f'<rect x="{(border + start) * scale}" y="{(border + r) * scale}" '
                f'width="{(c - start) * scale}" height="{scale}"/>'
            )
    parts.append("</g></svg>")
    return "".join(parts)


def svg_for(text: str, ec: str = "M", scale: int = 8, border: int = 4) -> str:
    """文本 → SVG 字符串。"""
    return to_svg(encode_matrix(text, ec), scale=scale, border=border)


def data_url(text: str, ec: str = "M", scale: int = 8, border: int = 4) -> str:
    """文本 → `data:image/svg+xml;base64,...`，前端可直接塞进 <img src>。"""
    import base64
    svg = svg_for(text, ec=ec, scale=scale, border=border)
    return ("data:image/svg+xml;base64,"
            + base64.b64encode(svg.encode("utf-8")).decode("ascii"))
