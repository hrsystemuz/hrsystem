"""Генератор QR-кодов без внешних библиотек.

Байтовый режим, уровень коррекции ошибок M, версии 1–10 (до 213 байт —
для адреса проверки приказа этого с запасом достаточно).
Возвращает SVG, который вставляется прямо в страницу.
"""

# версия: (всего кодовых слов, кодовых слов коррекции на блок, число блоков)
_VERSIONS_M = {
    1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2),
    5: (134, 24, 2), 6: (172, 16, 4), 7: (196, 18, 4), 8: (242, 22, 4),
    9: (292, 22, 5), 10: (346, 26, 5),
}
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}


def _data_capacity(ver):
    total, ecc, blocks = _VERSIONS_M[ver]
    return total - ecc * blocks


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _bit(x, i):
    return (x >> i) & 1


def _encode_codewords(payload, ver):
    cap = _data_capacity(ver)
    bits = [0, 1, 0, 0]  # режим: байты
    count_bits = 8 if ver < 10 else 16
    bits += [_bit(len(payload), i) for i in range(count_bits - 1, -1, -1)]
    for b in payload:
        bits += [_bit(b, i) for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    return data


def _add_ecc_and_interleave(data, ver):
    total, ecc_len, nblocks = _VERSIONS_M[ver]
    nshort = nblocks - total % nblocks
    short_len = total // nblocks
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = data[k:k + short_len - ecc_len + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= nshort:
                result.append(blk[i])
    return result


class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        n = self.size
        self.m = [[False] * n for _ in range(n)]
        self.fn = [[False] * n for _ in range(n)]
        self._draw_function_patterns()

    def _set(self, x, y, dark):
        self.m[y][x] = bool(dark)
        self.fn[y][x] = True

    def _draw_function_patterns(self):
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for (cx, cy) in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = cx + dx, cy + dy
                    if 0 <= xx < n and 0 <= yy < n:
                        self._set(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.ver]
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        self._draw_version()

    def _draw_format(self, mask):
        n = self.size
        data = (0 << 3) | mask  # уровень M = 00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        for i in range(0, 6):
            self._set(8, i, _bit(bits, i))
        self._set(8, 7, _bit(bits, 6))
        self._set(8, 8, _bit(bits, 7))
        self._set(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set(14 - i, 8, _bit(bits, i))
        for i in range(0, 8):
            self._set(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, _bit(bits, i))
        self._set(8, n - 8, True)

    def _draw_version(self):
        if self.ver < 7:
            return
        n = self.size
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        for i in range(18):
            b = _bit(bits, i)
            a, c = n - 11 + i % 3, i // 3
            self._set(a, c, b)
            self._set(c, a, b)

    def draw_codewords(self, codewords):
        n = self.size
        i = 0
        total_bits = len(codewords) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total_bits:
                        self.m[y][x] = bool(_bit(codewords[i >> 3], 7 - (i & 7)))
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        n = self.size
        for y in range(n):
            for x in range(n):
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv and not self.fn[y][x]:
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m = self.size, self.m
        score = 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
            s = "".join("1" if v else "0" for v in line)
            for pat in ("00001011101", "10111010000"):
                start = s.find(pat)
                while start != -1:
                    score += 40
                    start = s.find(pat, start + 1)
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        k = abs(dark * 20 - n * n * 10) // (n * n)
        score += k * 10
        return score


def make_matrix(text):
    payload = text.encode("utf-8")
    for ver in range(1, 11):
        capacity = _data_capacity(ver) - (2 if ver < 10 else 3)
        if len(payload) <= capacity:
            break
    else:
        raise ValueError("Текст слишком длинный для QR-кода")
    codewords = _add_ecc_and_interleave(_encode_codewords(payload, ver), ver)
    best = None
    for mask in range(8):
        mx = _Matrix(ver)
        mx.draw_codewords(codewords)
        mx.apply_mask(mask)
        mx._draw_format(mask)
        p = mx.penalty()
        if best is None or p < best[0]:
            best = (p, mx)
    return [row[:] for row in best[1].m]


def svg(text, color="#10265c", border=2):
    """QR-код в виде SVG-разметки (прозрачный фон)."""
    m = make_matrix(text)
    n = len(m)
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    size = n + 2 * border
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-border} {-border} {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR-код"><path fill="{color}" '
        f'd="{"".join(parts)}"/></svg>'
    )
