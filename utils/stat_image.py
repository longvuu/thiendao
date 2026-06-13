"""
utils/stat_image.py — Tạo ảnh thuộc tính nhân vật
NOTE: File này hiện chưa được sử dụng (dead code).
      Giữ lại cho kế hoạch hiển thị stat dạng ảnh trong tương lai.
"""
import os, io
from PIL import Image, ImageDraw, ImageFont

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "stats")

def _find_font(bold: bool = False) -> str:
    """Tìm font có hỗ trợ tiếng Việt trên Windows/Linux/Mac."""
    import sys
    candidates = []
    if sys.platform == "win32":
        win = os.environ.get("WINDIR", "C:\\Windows")
        candidates = [
            os.path.join(win, "Fonts", "arial.ttf")    if not bold else os.path.join(win, "Fonts", "arialbd.ttf"),
            os.path.join(win, "Fonts", "segoeui.ttf")  if not bold else os.path.join(win, "Fonts", "segoeuib.ttf"),
            os.path.join(win, "Fonts", "tahoma.ttf")   if not bold else os.path.join(win, "Fonts", "tahomabd.ttf"),
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None  # fallback to PIL default

FONT_REG  = _find_font(bold=False)
FONT_BOLD = _find_font(bold=True)

BG_COLOR    = (68, 58, 50, 255)
LABEL_COLOR = (210, 205, 195)
VALUE_COLOR = (230, 225, 215)
TITLE_REG   = (200, 195, 185)
TITLE_BOLD  = (240, 235, 220)

SCALE   = 2
TITLE_H = 22 * SCALE
ROW_H   = 46 * SCALE
COL_W   = 90 * SCALE
PAD_L   = 8  * SCALE
PAD_T   = 4  * SCALE
ICON_SZ = 16 * SCALE

STAT_GRID = [
    [("Sinh lực",  "sinh_luc.png",  "sinh_luc"),
     ("Linh lực",  "linh_luc.png",  "linh_luc"),
     ("Tấn công",  "cong_kich.png", "cong_kich")],
    [("Phòng ngự", "phong_ngu.png", "phong_ngu"),
     ("Hội tâm",   "hoi_tam.png",  "hoi_tam"),
     ("Hộ tâm",   "ho_tam.png",   "ho_tam")],
    [("Bạo kích",  "bao_kich.png",  "bao_kich"),
     ("Kháng bạo", "khang_bao.png", "khang_bao"),
     None],
]

_icon_cache: dict = {}

def _get_icon(fname: str) -> Image.Image:
    if fname not in _icon_cache:
        ic = Image.open(os.path.join(_ASSETS, fname)).convert("RGBA")
        ic = ic.resize((ICON_SZ, ICON_SZ), Image.LANCZOS)
        _icon_cache[fname] = ic
    return _icon_cache[fname]

def _fmt(v) -> str:
    if isinstance(v, str): return v
    return f"{v:,}"

def calc_stats_image(ts: dict, phap_bao_data: list, yeu_thu_data: list) -> dict:
    """Tính tất cả chỉ số hiển thị trong ảnh từ data nhân vật."""
    cg  = ts["canh_gioi"]
    cap = ts["cap_nho"]
    lv  = cg * 9 + cap

    sinh_luc  = ts.get("hp_max", 100)
    cong_kich = ts.get("cong", 10)
    phong_ngu = ts.get("thu", 5)

    for pb in phap_bao_data:
        cong_kich += pb.get("at", 0)
        phong_ngu += pb.get("df", 0)

    for yt in yeu_thu_data:
        sinh_luc  += yt.get("hp_bonus", 0)
        cong_kich += yt.get("at_bonus", 0)
        phong_ngu += yt.get("df_bonus", 0)

    linh_luc  = int(sinh_luc * 0.12 + lv * 50)
    hoi_tam   = int(cong_kich * 0.08 + lv * 3)
    ho_tam    = int(phong_ngu * 0.15 + lv * 2)
    bao_kich  = min(5 + cg * 3 + cap, 75)
    khang_bao = min(3 + cg * 2 + cap // 2, 50)

    return {
        "sinh_luc":  sinh_luc,
        "linh_luc":  linh_luc,
        "cong_kich": cong_kich,
        "phong_ngu": phong_ngu,
        "hoi_tam":   hoi_tam,
        "ho_tam":    ho_tam,
        "bao_kich":  f"{bao_kich}%",
        "khang_bao": f"{khang_bao}%",
    }

def build_stat_image(stats: dict, canh_gioi_ten: str) -> bytes:
    W = COL_W * 3 + PAD_L
    H = TITLE_H + ROW_H * 3 + PAD_T

    img  = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    fs   = SCALE

    def _tf(path, size):
        if path: return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    f_title_reg  = _tf(FONT_REG,  12*fs)
    f_title_bold = _tf(FONT_BOLD, 12*fs)
    f_label      = _tf(FONT_REG,  10*fs)
    f_value      = _tf(FONT_BOLD, 14*fs)

    # Title
    ty = PAD_T + 2*fs
    draw.text((PAD_L, ty), "Cảnh giới hiện tại: ", font=f_title_reg, fill=TITLE_REG)
    prefix_w = int(f_title_reg.getlength("Cảnh giới hiện tại: "))
    draw.text((PAD_L + prefix_w, ty), canh_gioi_ten, font=f_title_bold, fill=TITLE_BOLD)

    # Grid
    for ri, row in enumerate(STAT_GRID):
        row_y = TITLE_H + ri * ROW_H
        for ci, cell in enumerate(row):
            if not cell:
                continue
            label, fname, key = cell
            cx   = PAD_L + ci * COL_W
            icon = _get_icon(fname)
            img.paste(icon, (cx, row_y + 5*fs), icon)
            draw.text((cx + ICON_SZ + 4*fs, row_y + 7*fs), label, font=f_label, fill=LABEL_COLOR)
            draw.text((cx, row_y + ICON_SZ + 9*fs), _fmt(stats.get(key, 0)), font=f_value, fill=VALUE_COLOR)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
