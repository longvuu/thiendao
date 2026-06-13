"""
utils/bar_renderer.py
Ghép các mảnh PNG thành 1 ảnh thanh máu/linh lực hoàn chỉnh.
Trả về discord.File để đính kèm vào embed.
NOTE: File này hiện chưa được sử dụng (dead code).
      Giữ lại cho kế hoạch render thanh HP/LL bằng ảnh trong tương lai.
"""
import os
import io
from PIL import Image
import discord

_BARS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "bars")

# Cache ảnh vào memory để tránh đọc file liên tục
_cache: dict[str, Image.Image] = {}

def _load(name: str) -> Image.Image:
    if name not in _cache:
        path = os.path.join(_BARS_DIR, name)
        _cache[name] = Image.open(path).convert("RGBA")
    return _cache[name].copy()

def _make_bar(val: int, mx: int, prefix: str) -> Image.Image:
    """
    Ghép 10 mảnh (left + mid1..8 + right) thành 1 ảnh.
    prefix: "hp" hoặc "ll"
    """
    filled = round(min(val / mx, 1.0) * 10) if mx > 0 else 10

    names = [f"{prefix}_left"] + [f"{prefix}_mid_{i}" for i in range(1, 9)] + [f"{prefix}_right"]
    pieces = []
    for j, name in enumerate(names):
        fname = f"{name}.png" if j < filled else f"{name}_empty.png"
        pieces.append(_load(fname))

    total_w = sum(p.width for p in pieces)
    h = pieces[0].height
    canvas = Image.new("RGBA", (total_w, h), (0, 0, 0, 0))
    x = 0
    for p in pieces:
        canvas.paste(p, (x, 0), p)
        x += p.width
    return canvas


def render_bars(
    hp_cur: int, hp_max: int,
    ll_cur: int, ll_max: int,
    label_top: str = "",
    label_bot: str = "",
) -> "discord.File":
    """
    Tạo ảnh gồm 2 thanh (sinh lực + linh lực) xếp dọc.
    Trả về discord.File với filename="bars.png".
    """
    bar_hp = _make_bar(hp_cur, hp_max, "hp")
    bar_ll = _make_bar(ll_cur, ll_max, "ll")

    W = max(bar_hp.width, bar_ll.width)
    GAP = 4  # px giữa 2 thanh
    H = bar_hp.height + GAP + bar_ll.height

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.paste(bar_hp, (0, 0), bar_hp)
    canvas.paste(bar_ll, (0, bar_hp.height + GAP), bar_ll)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="bars.png")
