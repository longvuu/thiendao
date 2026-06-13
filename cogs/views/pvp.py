"""
cogs/views/pvp.py
══════════════════════════════════════════════════════
PvP 1v1 — Thách đấu tu sĩ khác, tùy chọn cược LT.

Flow:
  1. /pvp @target hoặc nút ⚔️ trên hồ sơ → Modal nhập cược
  2. Gửi PUBLIC message (mention target) kèm PvPConfirmView
  3. Target bấm Chấp nhận → edit message → chạy combat public
  4. Kết quả PUBLIC trong cùng channel, mention cả 2
  5. Từ chối / Timeout → edit message thông báo

Cooldown: 300s per user (in-memory)
Giới hạn hạng: chênh lệch canh_gioi ≤ 2
Đánh tới khi 1 bên hết máu (giới hạn an toàn 200 hiệp)

Tất cả tin nhắn sau khi combat đều PUBLIC (không ephemeral).
Chỉ error/validation gửi ephemeral.
"""
from __future__ import annotations

import asyncio
import random
import time
import logging

import discord

from cogs.views._common import (
    get_tu_si, update_tu_si, add_linh_thach,
    fmt, bar, get_cg, get_cg_ten,
    _calc_full_stats,
    E_SINH_LUC, E_LINH_THACH, E_TU_VI,
    safe_followup,
)
from utils.embeds import e_loi, e_ok, e_warn

log = logging.getLogger("pvp")

# ── Config ──────────────────────────────────────────────────────
MAX_HIEP_SAFETY = 200  # giới hạn an toàn, tránh vòng lặp vô tận
PVP_CD_SECS   = 300   # 5 phút
MAX_CG_DIFF   = 2     # chênh lệch canh_gioi tối đa
BASE_EXP_WIN  = 300   # Tu Vi thưởng thắng (×(1+cg×0.1))
BASE_LT_WIN   = 500   # LT thưởng thắng ngoài cược
MAX_CUOC      = 10_000_000  # Giới hạn cược tối đa (10 triệu LT)

SKILL_ORDER = ["than_thong", "tuyet_ky", "than_phap", "vo_ky"]
LOAI_CD     = {"vo_ky": 2, "than_phap": 3, "tuyet_ky": 4, "than_thong": 5}
LOAI_DMGM   = {"vo_ky": 1.0, "than_phap": 1.0, "tuyet_ky": 1.6, "than_thong": 2.5}
FALLBACK_SK = {
    "vo_ky": "Quyền Cước", "than_phap": "Thân Pháp",
    "tuyet_ky": "Tuyệt Kỹ", "than_thong": "Thần Thông",
}

_pvp_cd:  dict[int, float] = {}   # {user_id: last_ts}
_pending: dict[int, bool]  = {}   # {challenger_id: True}

def _cleanup_pvp_dicts():
    """Dọn entries cũ — gọi mỗi khi dict > 500 entries."""
    cutoff = time.time() - PVP_CD_SECS * 2
    for uid in [u for u, t in list(_pvp_cd.items()) if t < cutoff]:
        _pvp_cd.pop(uid, None)


# ══════════════════════════════════════════════════════════════
#  COMBAT ENGINE
# ══════════════════════════════════════════════════════════════
def _compute_pvp(ts_a: dict, ts_b: dict) -> tuple[list, int, int]:
    """
    Turn-based combat giữa A và B, tối đa MAX_HIEP hiệp.
    Trả về (logs, hp_max_a, hp_max_b).
    log item: (hiep, dmg_a, crit_a, sk_a, dmg_b, crit_b, sk_b, hp_a, hp_b, line)
    """
    from cogs.cong_phap import get_cp_active, PHAM_DMG_MULT, CAP_DMG_MULT, LOAI_SK

    def _build(ts):
        full = _calc_full_stats(ts)
        cp   = get_cp_active(ts)
        pm   = 1.0
        sn   = {}; sl = {}
        if cp:
            pm = PHAM_DMG_MULT.get(cp["pham"], 1.0) * CAP_DMG_MULT.get(cp["cap"], 1.0)
            for loai in LOAI_SK:
                sk = cp["ky_nang"].get(loai)
                if sk:
                    sn[loai] = sk["ten"]
                    sl[loai] = sk.get("ll", 0)
        hp = full.get("hp_eff", ts["hp_max"])
        return {
            "at": full.get("at", ts["cong"]),
            "df": full.get("df", full.get("def", ts["thu"])),
            "hp_max": hp, "hp": hp,
            "hoi_tam":   int(full.get("hoi_tam", 0)),
            "ho_tam":    int(full.get("ho_tam",  0)),
            "bao_kich":  full.get("bao_kich",  0.0),
            "khang_bao": full.get("khang_bao", 0.0),
            "ll_max": ts.get("linh_luc", 100),
            "ll":     ts.get("linh_luc", 100),
            "pm": pm, "sn": sn, "sl": sl,
            "cd": {k: 0 for k in LOAI_SK},
        }

    def _atk(a, d):
        # Chọn skill
        sk = "vo_ky"
        for s in SKILL_ORDER:
            if a["cd"].get(s, 0) == 0 and a["ll"] >= a["sl"].get(s, 0):
                sk = s; break
        a["ll"] = max(0, a["ll"] - a["sl"].get(sk, 0))
        a["cd"][sk] = LOAI_CD.get(sk, 2)
        name = a["sn"].get(sk, FALLBACK_SK.get(sk, "Tấn Công"))
        mul  = LOAI_DMGM.get(sk, 1.0) * a["pm"]
        dr   = min(0.65, d["df"] / (d["df"] + a["at"] * 2 + 1))
        dmg  = max(1, int(a["at"] * mul * random.uniform(0.85, 1.15) * (1 - dr)))
        cr   = random.random() < max(0.05, min(0.75, a["hoi_tam"] / 1000 + a["bao_kich"]))
        if cr:
            dmg = int(dmg * 1.8 * (1 - min(0.80, d["khang_bao"])))
        return dmg, cr, name

    fa = _build(ts_a)
    fb = _build(ts_b)
    logs = []

    hiep = 0
    while fa["hp"] > 0 and fb["hp"] > 0 and hiep < MAX_HIEP_SAFETY:
        hiep += 1
        # Hồi LL + giảm CD
        fa["ll"] = min(fa["ll_max"], fa["ll"] + max(1, fa["ll_max"] // 20))
        fb["ll"] = min(fb["ll_max"], fb["ll"] + max(1, fb["ll_max"] // 20))
        for k in fa["cd"]: fa["cd"][k] = max(0, fa["cd"][k] - 1)
        for k in fb["cd"]: fb["cd"][k] = max(0, fb["cd"][k] - 1)

        da, ca, ska = _atk(fa, fb)
        fb["hp"] = max(0, fb["hp"] - da)

        if fb["hp"] > 0:
            db, cb, skb = _atk(fb, fa)
            fa["hp"] = max(0, fa["hp"] - db)
        else:
            db, cb, skb = 0, False, "—"

        line = (
            f"**Hiệp {hiep}** ⚔️ *{ska}*: **+{fmt(da)}**{'⚡' if ca else ''} "
            f"| 🛡️ *{skb}*: **+{fmt(db)}**{'⚡' if cb else ''}"
        )
        logs.append((hiep, da, ca, ska, db, cb, skb, fa["hp"], fb["hp"], line))

    return logs, fa["hp_max"], fb["hp_max"]


# ══════════════════════════════════════════════════════════════
#  EMBED — LỜI THÁCH (public, kèm buttons)
# ══════════════════════════════════════════════════════════════
def _embed_challenge(
    challenger: discord.User, target: discord.User,
    ts_c: dict, ts_t: dict, cuoc: int,
) -> discord.Embed:
    cg_c = get_cg_ten(ts_c["canh_gioi"], ts_c["cap_nho"])
    cg_t = get_cg_ten(ts_t["canh_gioi"], ts_t["cap_nho"])
    cuoc_str = f"**{fmt(cuoc)} {E_LINH_THACH}**" if cuoc > 0 else "*(không cược)*"
    embed = discord.Embed(
        title="⚔️ LỜI THÁCH ĐẤU",
        description=(
            f"🗡️ **{challenger.display_name}** `{cg_c}`"
            f" thách đấu **{target.display_name}** `{cg_t}`\n\n"
            f"💰 Cược: {cuoc_str}\n"
            f"⏳ {target.mention} có **60 giây** để phản hồi"
        ),
        color=0xDC143C,
    )
    return embed


# ══════════════════════════════════════════════════════════════
#  EMBED — KẾT QUẢ COMBAT (public)
# ══════════════════════════════════════════════════════════════
def _hp_bars(
    challenger: discord.User, target: discord.User,
    ts_c: dict, ts_t: dict,
    hp_a: int, hp_b: int, hp_max_a: int, hp_max_b: int,
) -> tuple:
    """Trả về 2 field value cho HP bar của cả 2 chiến sĩ."""
    cg_a = get_cg(ts_c["canh_gioi"])
    cg_b = get_cg(ts_t["canh_gioi"])
    val_a = f"`{bar(hp_a, hp_max_a)}` {fmt(hp_a)}/{fmt(hp_max_a)} {E_SINH_LUC}"
    val_b = f"`{bar(hp_b, hp_max_b)}` {fmt(hp_b)}/{fmt(hp_max_b)} {E_SINH_LUC}"
    name_a = f"{cg_a['emoji']} {challenger.display_name}"
    name_b = f"{cg_b['emoji']} {target.display_name}"
    return (name_a, val_a), (name_b, val_b)


def _embed_live(
    challenger: discord.User, target: discord.User,
    ts_c: dict, ts_t: dict,
    logs_so_far: list, hp_max_a: int, hp_max_b: int,
    total_hiep: int,
) -> discord.Embed:
    """Embed hiển thị tiến trình từng hiệp — dùng để edit message khi đang đánh."""
    last = logs_so_far[-1] if logs_so_far else None
    hp_a = last[7] if last else hp_max_a
    hp_b = last[8] if last else hp_max_b
    hiep_now = last[0] if last else 0

    embed = discord.Embed(
        title="⚔️ ĐANG CHIẾN ĐẤU...",
        description=f"Hiệp **{hiep_now}** / {total_hiep}",
        color=0xDC143C,
    )
    (na, va), (nb, vb) = _hp_bars(challenger, target, ts_c, ts_t,
                                   hp_a, hp_b, hp_max_a, hp_max_b)
    embed.add_field(name=na, value=va, inline=True)
    embed.add_field(name=nb, value=vb, inline=True)

    # Hiện tối đa 5 hiệp gần nhất
    show = logs_so_far[-5:]
    embed.add_field(
        name="📜 Diễn biến",
        value="\n".join(l[9] for l in show) or "*(bắt đầu...)*",
        inline=False,
    )
    embed.set_footer(text=f"PvP · {challenger.display_name} vs {target.display_name}")
    return embed


def _embed_final(
    challenger: discord.User, target: discord.User,
    ts_c: dict, ts_t: dict,
    logs: list, hp_max_a: int, hp_max_b: int,
    cuoc: int, winner_id: int | None, lt_won: int, exp_won: int,
) -> discord.Embed:
    """Embed kết quả cuối sau khi combat xong."""
    hp_a = logs[-1][7] if logs else hp_max_a
    hp_b = logs[-1][8] if logs else hp_max_b
    won_a = (winner_id == challenger.id)
    won_b = (winner_id == target.id)

    if won_a:
        title, color = f"🏆 {challenger.display_name} ĐẠI THẮNG!", 0xFFD700
    elif won_b:
        title, color = f"🏆 {target.display_name} ĐẠI THẮNG!", 0xFFD700
    else:
        title, color = "⚖️ HÒA — Cân Sức!", 0x888888

    embed = discord.Embed(title=title, color=color)
    (na, va), (nb, vb) = _hp_bars(challenger, target, ts_c, ts_t,
                                   hp_a, hp_b, hp_max_a, hp_max_b)
    embed.add_field(name=na, value=va, inline=True)
    embed.add_field(name=nb, value=vb, inline=True)

    # Toàn bộ log (tối đa 10 hiệp cuối để tránh vượt giới hạn Discord)
    show = logs[-10:] if len(logs) > 10 else logs
    log_text = "\n".join(l[9] for l in show)
    if len(logs) > 10:
        log_text = f"*… (bỏ qua {len(logs)-10} hiệp đầu)*\n" + log_text
    embed.add_field(name="📜 Toàn bộ diễn biến", value=log_text or "*(trống)*", inline=False)

    # Phần thưởng
    if winner_id:
        wname = challenger.display_name if won_a else target.display_name
        lname = target.display_name    if won_a else challenger.display_name
        parts = [f"🏆 **{wname}** thắng"]
        if lt_won:  parts.append(f"{E_LINH_THACH} +{fmt(lt_won)}")
        if exp_won: parts.append(f"{E_TU_VI} +{fmt(exp_won)}")
        if cuoc:    parts.append(f"💸 {lname} mất {fmt(cuoc)} cược")
        embed.add_field(name="🎖️ Kết quả", value=" · ".join(parts), inline=False)
    else:
        embed.add_field(name="🎖️ Kết quả", value="Hòa — không trao thưởng", inline=False)

    embed.set_footer(text=f"PvP · {len(logs)} hiệp · {challenger.display_name} vs {target.display_name}")
    return embed


# ══════════════════════════════════════════════════════════════
#  VIEW: XÁC NHẬN (gắn vào public message)
# ══════════════════════════════════════════════════════════════
class PvPConfirmView(discord.ui.View):
    def __init__(
        self,
        challenger: discord.User,
        target: discord.User,
        ts_c: dict,
        ts_t: dict,
        cuoc: int,
    ):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.target     = target
        self.ts_c       = ts_c
        self.ts_t       = ts_t
        self.cuoc       = cuoc
        self._done      = False

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="⚔️ Chấp nhận", style=discord.ButtonStyle.success)
    async def btn_accept(self, inter: discord.Interaction, _btn):
        if inter.user.id != self.target.id:
            return await inter.response.send_message(
                "❌ Đây không phải lời thách của bạn!", ephemeral=True)
        if self._done:
            return await inter.response.send_message("❌ Đã xử lý rồi!", ephemeral=True)
        self._done = True
        self.stop()
        self._disable_all()

        # Edit message thách đấu → "Đang chiến đấu..."
        try:
            await inter.response.edit_message(
                content=f"⚔️ **{self.target.display_name}** chấp nhận! Đang tính toán...",
                embed=None,
                view=self,
            )
        except Exception:
            log.exception("Lỗi pvp")

        # Chạy combat và gửi kết quả PUBLIC vào channel
        await _run_combat_public(inter, self.challenger, self.target,
                                 self.ts_c, self.ts_t, self.cuoc)

    @discord.ui.button(label="🏳️ Từ chối", style=discord.ButtonStyle.danger)
    async def btn_reject(self, inter: discord.Interaction, _btn):
        if inter.user.id != self.target.id:
            return await inter.response.send_message(
                "❌ Đây không phải lời thách của bạn!", ephemeral=True)
        if self._done:
            return await inter.response.send_message("❌ Đã xử lý rồi!", ephemeral=True)
        self._done = True
        self.stop()
        self._disable_all()
        _pending.pop(self.challenger.id, None)
        try:
            await inter.response.edit_message(
                content=(
                    f"🏳️ **{self.target.display_name}** từ chối lời thách đấu "
                    f"của **{self.challenger.display_name}**."
                ),
                embed=None,
                view=self,
            )
        except Exception:
            log.exception("Lỗi pvp")

    async def on_timeout(self):
        if self._done:
            return
        self._done = True
        _pending.pop(self.challenger.id, None)
        self._disable_all()
        # Cố edit message timeout — không có inter nên dùng message nếu có
        # discord.py sẽ tự stop view; message sẽ giữ nguyên (buttons disabled)


# ══════════════════════════════════════════════════════════════
#  COMBAT RUNNER — PUBLIC
# ══════════════════════════════════════════════════════════════
# Delay giữa các lần edit (giây) — đủ chậm để người xem theo dõi
HIEP_DELAY = 1.5


async def _run_combat_public(
    inter: discord.Interaction,
    challenger: discord.User,
    target: discord.User,
    ts_c: dict,
    ts_t: dict,
    cuoc: int,
):
    """
    Stream combat từng hiệp PUBLIC:
    1. Tính toàn bộ log trước (trong executor)
    2. Gửi 1 message với embed hiệp 1
    3. Edit message sau mỗi HIEP_DELAY giây để hiện hiệp tiếp theo
    4. Edit lần cuối với embed kết quả + phần thưởng
    """
    _pending.pop(challenger.id, None)

    loop = asyncio.get_running_loop()
    try:
        logs, hp_max_a, hp_max_b = await loop.run_in_executor(
            None, _compute_pvp, ts_c, ts_t
        )
    except Exception as e:
        log.error(f"PvP combat error: {e}", exc_info=True)
        try:
            await inter.channel.send(f"❌ PvP lỗi tính toán: {e}")
        except Exception:
            log.exception("Lỗi pvp")
        return

    total_hiep = len(logs)

    # Xác định winner
    hp_a = logs[-1][7] if logs else hp_max_a
    hp_b = logs[-1][8] if logs else hp_max_b

    if   hp_a > 0 and hp_b <= 0: winner_id, loser_id = challenger.id, target.id
    elif hp_b > 0 and hp_a <= 0: winner_id, loser_id = target.id, challenger.id
    elif hp_a > hp_b:             winner_id, loser_id = challenger.id, target.id
    elif hp_b > hp_a:             winner_id, loser_id = target.id, challenger.id
    else:                         winner_id = loser_id = None

    # Tính thưởng
    lt_won = exp_won = 0
    if winner_id:
        winner_ts = ts_c if winner_id == challenger.id else ts_t
        cg_w = winner_ts["canh_gioi"]
        exp_won = int(BASE_EXP_WIN * (1 + cg_w * 0.1))
        lt_won  = int(BASE_LT_WIN  * (1 + cg_w * 0.1))
        if cuoc > 0:
            lt_won += cuoc

    # CD cho cả 2
    _pvp_cd[challenger.id] = time.time()
    _pvp_cd[target.id]     = time.time()
    if len(_pvp_cd) > 500: _cleanup_pvp_dicts()

    # Cập nhật DB (chạy song song với animation)
    async def _update_db():
        try:
            if winner_id and loser_id:
                from utils.database import get_tu_si as _get_ts, log_giao_dich as _log_gd
                winner_ts = ts_c if winner_id == challenger.id else ts_t
                loser_ts  = ts_t if winner_id == challenger.id else ts_c
                await add_linh_thach(winner_id, lt_won)
                await update_tu_si(winner_id,
                    exp=winner_ts["exp"] + exp_won,
                    thang_pvp=winner_ts.get("thang_pvp", 0) + 1)
                await update_tu_si(loser_id,
                    thua_pvp=loser_ts.get("thua_pvp", 0) + 1)
                if cuoc > 0:
                    # Re-fetch loser LT để tránh số âm
                    loser_fresh = await _get_ts(loser_id)
                    cuoc_thuc   = min(cuoc, loser_fresh.get("linh_thach", 0)) if loser_fresh else cuoc
                    if cuoc_thuc > 0:
                        await add_linh_thach(loser_id, -cuoc_thuc)
                        # Ghi log PvP cược để rollback khi van dinh (coi như giao dịch)
                        await _log_gd(
                            "pvp_cuoc",
                            sender_id   = loser_id,
                            receiver_id = winner_id,
                            item_name   = "PvP Cược",
                            so_luong    = 1,
                            gia_lt      = cuoc_thuc,
                            item_loai   = "pvp_cuoc",
                            item_key    = "",
                            ghi_chu     = f"pvp_cuoc winner={winner_id}",
                        )
        except Exception as e:
            log.error(f"PvP DB error: {e}", exc_info=True)

    db_task = asyncio.create_task(_update_db())

    # ── Helper gửi/edit message — fallback về followup nếu channel bị Forbidden ──
    async def _send(content, embed):
        """Gửi message mới. Ưu tiên channel.send, fallback followup nếu Forbidden."""
        try:
            return await inter.channel.send(content=content, embed=embed)
        except discord.Forbidden:
            log.debug("PvP: Forbidden trên channel.send, dùng followup")
            try:
                return await inter.followup.send(content=content, embed=embed)
            except Exception as e2:
                log.error(f"PvP: followup cũng lỗi: {e2}")
                return None
        except Exception as e:
            log.error(f"PvP: send lỗi: {e}", exc_info=True)
            return None

    async def _edit(msg, content=None, embed=None):
        """Edit message. Trả về False nếu lỗi không thể tiếp tục."""
        if msg is None:
            return False
        try:
            kwargs = {}
            if content is not None: kwargs["content"] = content
            if embed   is not None: kwargs["embed"]   = embed
            await msg.edit(**kwargs)
            return True
        except (discord.NotFound, discord.Forbidden) as e:
            log.warning(f"PvP: edit thất bại ({type(e).__name__}), bỏ animation")
            return False
        except Exception as e:
            log.warning(f"PvP: edit lỗi: {e}")
            return False

    # ── Gửi message đầu tiên với hiệp 1 ──────────────────────
    combat_msg = await _send(
        content=f"{challenger.mention} vs {target.mention}",
        embed=_embed_live(challenger, target, ts_c, ts_t,
                          logs[:1], hp_max_a, hp_max_b, total_hiep),
    )
    if combat_msg is None:
        await db_task
        return

    # ── Edit từng hiệp tiếp theo ──────────────────────────────
    for i in range(2, total_hiep + 1):
        await asyncio.sleep(HIEP_DELAY)
        ok = await _edit(combat_msg,
            embed=_embed_live(challenger, target, ts_c, ts_t,
                              logs[:i], hp_max_a, hp_max_b, total_hiep))
        if not ok:
            break  # edit thất bại → bỏ qua animation, xuống kết quả

    # ── Edit lần cuối: embed kết quả đầy đủ ──────────────────
    await asyncio.sleep(HIEP_DELAY)
    embed_end = _embed_final(
        challenger, target, ts_c, ts_t,
        logs, hp_max_a, hp_max_b,
        cuoc, winner_id, lt_won, exp_won,
    )
    ok = await _edit(combat_msg,
        content=f"{challenger.mention} {target.mention}",
        embed=embed_end)
    if not ok:
        # edit final thất bại → gửi message mới
        await _send(content=f"{challenger.mention} {target.mention}", embed=embed_end)

    # Đảm bảo DB task xong
    try:
        await db_task
    except Exception:
        log.exception("Lỗi pvp")


# ══════════════════════════════════════════════════════════════
#  TRIGGER VIEW: mở modal sau khi interaction đã được defer
# ══════════════════════════════════════════════════════════════
class _CuocTriggerView(discord.ui.View):
    """Gửi ephemeral button để mở PvPCuocModal từ interaction mới (tránh 404)."""

    def __init__(self, challenger: discord.User, target: discord.User,
                 ts_c: dict, ts_t: dict):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.target     = target
        self.ts_c       = ts_c
        self.ts_t       = ts_t

    @discord.ui.button(label="⚔️ Nhập Số Cược", style=discord.ButtonStyle.danger)
    async def nhap_cuoc(self, inter: discord.Interaction, button: discord.ui.Button):
        if inter.user.id != self.challenger.id:
            return await inter.response.send_message(
                "❌ Không phải cuộc đấu của bạn!", ephemeral=True)
        self.stop()
        await inter.response.send_modal(
            PvPCuocModal(self.challenger, self.target, self.ts_c, self.ts_t))


# ══════════════════════════════════════════════════════════════
#  MODAL: NHẬP CƯỢC
# ══════════════════════════════════════════════════════════════
class PvPCuocModal(discord.ui.Modal, title="⚔️ Thách Đấu PvP"):
    cuoc_input = discord.ui.TextInput(
        label="Số LT cược (0 = không cược)",
        placeholder="Nhập số linh thạch muốn cược, ví dụ: 5000",
        required=False,
        max_length=12,
        default="0",
    )

    def __init__(self, challenger: discord.User, target: discord.User,
                 ts_c: dict, ts_t: dict):
        super().__init__()
        self.challenger = challenger
        self.target     = target
        self.ts_c       = ts_c
        self.ts_t       = ts_t

    async def on_submit(self, inter: discord.Interaction):
        try:
            cuoc = max(0, int(self.cuoc_input.value.strip() or "0"))
        except ValueError:
            return await inter.response.send_message(
                "❌ Số không hợp lệ!", ephemeral=True)

        # Validate cược
        if cuoc > 0:
            if cuoc > MAX_CUOC:
                return await inter.response.send_message(
                    embed=e_loi("❌ Vượt Giới Hạn Cược",
                        f"Cược tối đa là **{fmt(MAX_CUOC)}** LT để tránh giao dịch bất chính."),
                    ephemeral=True)
            if self.ts_c["linh_thach"] < cuoc:
                return await inter.response.send_message(
                    embed=e_loi("❌ Không Đủ LT",
                        f"Bạn chỉ có **{fmt(self.ts_c['linh_thach'])}** LT, "
                        f"không đủ để cược **{fmt(cuoc)}** LT."),
                    ephemeral=True)
            if self.ts_t["linh_thach"] < cuoc:
                return await inter.response.send_message(
                    embed=e_warn("⚠️ Mục Tiêu Không Đủ LT",
                        f"**{self.target.display_name}** chỉ có "
                        f"**{fmt(self.ts_t['linh_thach'])}** LT, "
                        f"không đủ đáp cược **{fmt(cuoc)}** LT."),
                    ephemeral=True)

        _pending[self.challenger.id] = True

        # Gửi PUBLIC message với PvPConfirmView
        embed_req = _embed_challenge(
            self.challenger, self.target, self.ts_c, self.ts_t, cuoc)
        view = PvPConfirmView(
            self.challenger, self.target, self.ts_c, self.ts_t, cuoc)

        try:
            await inter.response.send_message(
                content=f"{self.target.mention}",
                embed=embed_req,
                view=view,
            )
        except Exception as e:
            _pending.pop(self.challenger.id, None)
            log.error(f"PvP: không gửi challenge: {e}", exc_info=True)
            return

        # Timeout cleanup
        async def _cleanup():
            await asyncio.sleep(65)
            _pending.pop(self.challenger.id, None)
        asyncio.create_task(_cleanup())


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════
async def start_pvp(inter: discord.Interaction, target: discord.User):
    """
    Entry point từ /pvp hoặc nút trong hồ sơ.
    Validate rồi mở modal nhập cược.
    """
    challenger = inter.user

    if challenger.id == target.id:
        return await inter.response.send_message(
            embed=e_loi("❌ Không thể tự thách mình", "Hãy thách người khác!"),
            ephemeral=True)

    if target.bot:
        return await inter.response.send_message(
            embed=e_loi("❌ Không thể thách bot", "Bot không biết đánh nhau!"),
            ephemeral=True)

    # CD check
    now    = time.time()
    remain = PVP_CD_SECS - (now - _pvp_cd.get(challenger.id, 0))
    if remain > 0:
        m, s = int(remain) // 60, int(remain) % 60
        return await inter.response.send_message(
            embed=e_warn("⏳ Đang Hồi Sức",
                f"Cần nghỉ thêm **{m}p {s}s** trước khi thách đấu tiếp."),
            ephemeral=True)

    if _pending.get(challenger.id):
        return await inter.response.send_message(
            embed=e_warn("⏳ Đang Chờ", "Bạn đang có lời thách chưa được trả lời."),
            ephemeral=True)

    # Defer ngay trước khi truy vấn DB — tránh 404 Unknown interaction
    await inter.response.defer(ephemeral=True)

    ts_c, ts_t = await asyncio.gather(get_tu_si(challenger.id), get_tu_si(target.id))

    if not ts_c:
        return await inter.followup.send(
            embed=e_loi("❌ Chưa Tu Tiên", "Dùng **/hoso** để tạo hồ sơ trước."),
            ephemeral=True)
    if not ts_t:
        return await inter.followup.send(
            embed=e_loi("❌ Mục Tiêu Chưa Tu Tiên",
                f"**{target.display_name}** chưa có hồ sơ tu tiên!"),
            ephemeral=True)

    cg_diff = abs(ts_c["canh_gioi"] - ts_t["canh_gioi"])
    if cg_diff > MAX_CG_DIFF:
        return await inter.followup.send(
            embed=e_loi("❌ Chênh Lệch Quá Lớn",
                f"Chênh nhau **{cg_diff} cảnh giới** (tối đa {MAX_CG_DIFF})."),
            ephemeral=True)

    # Không thể send_modal sau defer — dùng nút bấm mở modal từ interaction mới
    await inter.followup.send(
        embed=discord.Embed(
            title="⚔️ Thách Đấu PvP",
            description=f"Nhấn nút bên dưới để nhập số LT cược và thách **{target.display_name}**.",
            color=0xE74C3C,
        ),
        view=_CuocTriggerView(challenger, target, ts_c, ts_t),
        ephemeral=True,
    )
