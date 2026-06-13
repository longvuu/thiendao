"""
private_trade.py
══════════════════════════════════════════════════════
Giao dịch riêng tư (Private Trade) giữa 2 người chơi.

Flow:
  1. Người gửi chọn item trong KhoDoView → bấm "🤝 Giao Dịch"
  2. Modal: nhập @user_id người nhận + số lượng + giá muốn bán (LT)
  3. Bot gửi ephemeral xác nhận cho người GỬI
  4. Bot gửi DM (hoặc ephemeral) cho người NHẬN với nút [✅ Chấp Nhận] / [❌ Từ Chối]
  5. Người nhận xác nhận → atomic transfer:
       - Trừ item khỏi người gửi
       - Trừ LT (giá) khỏi người nhận
       - Cộng item cho người nhận
       - Cộng LT × (1 - FEE) cho người gửi (FEE 15%)
  6. Ghi log giao dịch

Item được hỗ trợ: Nguyên Liệu, Đan Dược (dtl/dd), Linh Quả, Mảnh Linh Căn
Không hỗ trợ: Pháp Bảo, Sủng Thú, Linh Căn (non-transferable)

Fee: 15% trên giá bán (thu của người GỬI, không phải người mua).
Thời gian chờ xác nhận: 5 phút, sau đó offer tự hủy.
"""
from __future__ import annotations

import asyncio
import json
import time
import logging

import discord

from utils.bot_emojis import E_LINH_THACH
from utils.config import (
    NGUYEN_LIEU, DAN_DUOC, LINH_QUA, LINH_QUA_BY_ID,
    MANH_LINH_CAN_EMOJI, fmt,
)
from utils.database import (
    get_tu_si, update_tu_si, add_linh_thach, log_giao_dich,
)
from utils.embeds import e_ok, e_loi, e_warn, safe_followup

log = logging.getLogger("private_trade")

# ── Constants ──────────────────────────────────────────────────────
TRADE_FEE     = 0.15   # 15% phí, thu từ tiền người bán nhận được
TRADE_TIMEOUT = 300    # 5 phút người mua phải xác nhận

# item loại không cho trade
LOAI_BLOCK = {"Pháp Bảo", "Nguyên Liệu ĐP TC"}


# ══════════════════════════════════════════════════════════════
#  ATOMIC TRANSFER — tất cả item types
# ══════════════════════════════════════════════════════════════
async def _transfer_item_atomic(
    sender_id: int,
    target_id: int,
    item: dict,
    so_luong: int,
) -> tuple[bool, str]:
    """
    Chuyển so_luong item từ sender → target, atomic.
    Trả về (ok, error_message).
    """
    from utils.database import get_pool
    pool = await get_pool()
    loai = item.get("loai", "")

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── Lock cả 2 row theo thứ tự uid để tránh deadlock ──
            uid_lo, uid_hi = sorted([sender_id, target_id])
            row_lo = await conn.fetchrow(
                "SELECT user_id, dan_duoc, nguyen_lieu, linh_qua, manh_linh_can "
                "FROM tu_si WHERE user_id=$1 FOR UPDATE", uid_lo)
            row_hi = await conn.fetchrow(
                "SELECT user_id, dan_duoc, nguyen_lieu, linh_qua, manh_linh_can "
                "FROM tu_si WHERE user_id=$1 FOR UPDATE", uid_hi)
            if not row_lo or not row_hi:
                return False, "Không tìm thấy hồ sơ!"

            rows = {row_lo["user_id"]: row_lo, row_hi["user_id"]: row_hi}
            row_s = rows[sender_id]
            row_t = rows[target_id]

            def _parse(row, field):
                raw = row[field]
                if isinstance(raw, dict): return raw.copy()
                try: return json.loads(raw) if raw else {}
                except Exception: return {}

            # ── Xác định field và key ──────────────────────────
            if loai in ("Đan Tu Luyện", "Đan Dược"):
                field = "dan_duoc"
                key   = item.get("_dtl_key") or str(item.get("_dd_id", ""))
            elif loai == "Nguyên Liệu":
                field = "nguyen_lieu"
                key   = str(item.get("_nl_id", ""))
            elif loai == "Linh Quả":
                field = "linh_qua"
                key   = item.get("_lq_id", "")
            elif loai == "Mảnh Linh Căn":
                field = "manh_linh_can"
                key   = item.get("_lq_id", "")
            else:
                return False, f"Loại '{loai}' không hỗ trợ giao dịch!"

            kho_s = _parse(row_s, field)
            co    = kho_s.get(key, 0)
            if co < so_luong:
                return False, f"Người bán không đủ — chỉ còn {co}!"

            kho_t = _parse(row_t, field)
            kho_s[key] = co - so_luong
            if kho_s[key] <= 0:
                kho_s.pop(key, None)
            kho_t[key] = kho_t.get(key, 0) + so_luong

            await conn.execute(
                f"UPDATE tu_si SET {field}=$1 WHERE user_id=$2",
                json.dumps(kho_s), sender_id)
            await conn.execute(
                f"UPDATE tu_si SET {field}=$1 WHERE user_id=$2",
                json.dumps(kho_t), target_id)

    return True, ""


# ══════════════════════════════════════════════════════════════
#  MODAL — người gửi nhập thông tin
# ══════════════════════════════════════════════════════════════
class PrivateTradeModal(discord.ui.Modal, title="🤝 Giao Dịch Riêng Tư"):
    nguoi_nhan = discord.ui.TextInput(
        label="User ID người nhận",
        placeholder="Right-click → Copy User ID (cần bật Developer Mode)",
        min_length=5, max_length=22,
    )
    so_luong = discord.ui.TextInput(
        label="Số lượng",
        placeholder="1",
        min_length=1, max_length=6,
    )
    gia = discord.ui.TextInput(
        label="Giá (LT) — tối thiểu 500, fee 15%",
        placeholder="Tối thiểu: 500 LT",
        min_length=1, max_length=12,
    )

    def __init__(self, kho_view, item: dict):
        super().__init__()
        self.kho_view = kho_view
        self.item     = item
        self.so_luong.placeholder = f"Tối đa: {item.get('so_luong', 1)}"

    async def on_submit(self, inter: discord.Interaction):
        # ── Validate input (sync, không cần defer) ──────────────
        try:
            target_id = int(self.nguoi_nhan.value.strip())
        except ValueError:
            return await inter.response.send_message(
                "❌ User ID không hợp lệ — phải là số!", ephemeral=True)

        try:
            sl = int(self.so_luong.value.strip())
        except ValueError:
            return await inter.response.send_message("❌ Số lượng không hợp lệ!", ephemeral=True)

        try:
            gia_raw = self.gia.value.strip().replace(",", "").replace(".", "")
            gia = int(gia_raw)
        except ValueError:
            return await inter.response.send_message("❌ Giá không hợp lệ!", ephemeral=True)

        if sl < 1:
            return await inter.response.send_message("❌ Số lượng phải ≥ 1!", ephemeral=True)
        if sl > self.item.get("so_luong", 0):
            return await inter.response.send_message(
                f"❌ Chỉ có **{self.item['so_luong']}**, không đủ **{sl}**!", ephemeral=True)
        if gia < 500:
            return await inter.response.send_message("❌ Giá tối thiểu là **500 Linh Thạch**!", ephemeral=True)
        if target_id == inter.user.id:
            return await inter.response.send_message("❌ Không tự giao dịch với mình!", ephemeral=True)

        # ── Defer trước khi DB call ──────────────────────────────
        await inter.response.defer(ephemeral=True)

        # Kiểm tra target tồn tại
        ts_target = await get_tu_si(target_id)
        if not ts_target:
            return await safe_followup(inter, 
                "❌ Người nhận chưa có hồ sơ tu sĩ!", ephemeral=True)

        # Kiểm tra số lượng còn đủ (real-time)
        ts_sender = await get_tu_si(inter.user.id)
        item      = self.item
        loai      = item.get("loai", "")

        if loai in ("Đan Tu Luyện", "Đan Dược"):
            key = item.get("_dtl_key") or str(item.get("_dd_id", ""))
            co  = ts_sender.get("dan_duoc", {}).get(key, 0)
        elif loai == "Nguyên Liệu":
            key = str(item.get("_nl_id", ""))
            co  = ts_sender.get("nguyen_lieu", {}).get(key, 0)
        elif loai == "Linh Quả":
            key = item.get("_lq_id", "")
            co  = ts_sender.get("linh_qua", {}).get(key, 0)
        elif loai == "Mảnh Linh Căn":
            key = item.get("_lq_id", "")
            co  = ts_sender.get("manh_linh_can", {}).get(key, 0)
        else:
            return await safe_followup(inter, 
                f"❌ **{loai}** không hỗ trợ giao dịch riêng tư!", ephemeral=True)

        if co < sl:
            return await safe_followup(inter, 
                f"❌ Kho vừa thay đổi — chỉ còn **{co}**, không đủ **{sl}**!", ephemeral=True)

        # Tính phí
        phi     = int(gia * TRADE_FEE)
        nhan_ve = gia - phi

        # Gửi offer cho người nhận
        try:
            target_user = inter.client.get_user(target_id) or await inter.client.fetch_user(target_id)
        except Exception:
            return await safe_followup(inter, 
                "❌ Không tìm thấy người dùng với ID đó!", ephemeral=True)

        dao_hieu_sender  = ts_sender.get("dao_hieu", inter.user.display_name)
        dao_hieu_target  = ts_target.get("dao_hieu", target_user.display_name)
        item_name        = f"{item['emoji']} {item['ten']}"
        phi_pct          = int(TRADE_FEE * 100)

        # Build offer embed
        offer_embed = discord.Embed(
            title="🤝 Lời Mời Giao Dịch Riêng Tư",
            description=(
                f"**{dao_hieu_sender}** muốn bán cho bạn:"
            ),
            color=0x5865F2,
        )
        offer_embed.add_field(
            name="📦 Vật phẩm",
            value=f"{item_name} × **{sl}**",
            inline=True,
        )
        offer_embed.add_field(
            name=f"{E_LINH_THACH} Giá",
            value=f"**{fmt(gia)}** LT" if gia > 0 else "🎁 Miễn phí",
            inline=True,
        )
        offer_embed.add_field(
            name="⏳ Hết hạn",
            value=f"<t:{int(time.time()) + TRADE_TIMEOUT}:R>",
            inline=True,
        )
        if gia > 0:
            offer_embed.add_field(
                name=f"💸 Phí giao dịch ({phi_pct}%)",
                value=(
                    f"Người bán chịu phí: **{fmt(phi)} LT**\n"
                    f"Người bán nhận về: **{fmt(nhan_ve)} LT**"
                ),
                inline=False,
            )
        offer_embed.set_footer(text=f"Từ: {inter.user.display_name} ({inter.user.id})")

        # Build accept/reject view
        offer_view = _OfferView(
            sender_id      = inter.user.id,
            target_id      = target_id,
            item           = item,
            so_luong       = sl,
            gia            = gia,
            phi            = phi,
            nhan_ve        = nhan_ve,
            dao_hieu_sender= dao_hieu_sender,
            dao_hieu_target= dao_hieu_target,
            item_name      = item_name,
        )

        # Thử gửi DM trước, fallback ephemeral trong server
        dm_ok = False
        try:
            dm_msg = await target_user.send(embed=offer_embed, view=offer_view)
            offer_view._msg = dm_msg
            dm_ok = True
        except discord.Forbidden:
            pass

        if not dm_ok:
            # Fallback: gửi ephemeral trong channel nếu có guild
            if inter.guild:
                try:
                    fallback_msg = await inter.channel.send(
                        content=f"<@{target_id}>",
                        embed=offer_embed,
                        view=offer_view,
                    )
                    offer_view._msg = fallback_msg
                    dm_ok = True
                except Exception:
                    log.exception("Lỗi private_trade")

        if not dm_ok:
            return await safe_followup(inter, 
                "❌ Không gửi được lời mời — người nhận đã tắt DM và không thể tag trong server!",
                ephemeral=True,
            )

        # Xác nhận cho người gửi
        confirm_embed = discord.Embed(
            title="✅ Đã Gửi Lời Mời Giao Dịch!",
            description=(
                f"Đã gửi lời mời tới **{dao_hieu_target}**.\n"
                f"Lời mời hết hạn sau **{TRADE_TIMEOUT // 60} phút**.\n\n"
                f"**Vật phẩm:** {item_name} × {sl}\n"
                f"**Giá:** {fmt(gia)} LT" + (f"\n**Bạn nhận về:** {fmt(nhan_ve)} LT (sau phí {phi_pct}%)" if gia > 0 else "")
            ),
            color=0x57F287,
        )
        await safe_followup(inter, embed=confirm_embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  OFFER VIEW — gửi cho người nhận
# ══════════════════════════════════════════════════════════════
class _OfferView(discord.ui.View):
    def __init__(
        self,
        sender_id: int,
        target_id: int,
        item: dict,
        so_luong: int,
        gia: int,
        phi: int,
        nhan_ve: int,
        dao_hieu_sender: str,
        dao_hieu_target: str,
        item_name: str,
    ):
        super().__init__(timeout=TRADE_TIMEOUT)
        self.sender_id       = sender_id
        self.target_id       = target_id
        self.item            = item
        self.so_luong        = so_luong
        self.gia             = gia
        self.phi             = phi
        self.nhan_ve         = nhan_ve
        self.dao_hieu_sender = dao_hieu_sender
        self.dao_hieu_target = dao_hieu_target
        self.item_name       = item_name
        self._msg            = None
        self._done           = False

    async def on_timeout(self):
        if self._done:
            return
        self._done = True
        if self._msg:
            try:
                expire_embed = discord.Embed(
                    title="⏰ Lời Mời Đã Hết Hạn",
                    description="Giao dịch đã tự động hủy do quá thời gian chờ.",
                    color=0x888888,
                )
                await self._msg.edit(embed=expire_embed, view=None)
            except Exception:
                log.exception("Lỗi private_trade")

    @discord.ui.button(label="✅ Chấp Nhận", style=discord.ButtonStyle.success, row=0)
    async def btn_accept(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.target_id:
            return await inter.response.send_message(
                "❌ Lời mời này không dành cho bạn!", ephemeral=True)
        if self._done:
            return await inter.response.send_message(
                "❌ Giao dịch này đã xử lý rồi!", ephemeral=True)

        await inter.response.defer()
        self._done = True
        self.stop()

        # Kiểm tra LT người mua
        if self.gia > 0:
            ts_buyer = await get_tu_si(self.target_id)
            if not ts_buyer or ts_buyer["linh_thach"] < self.gia:
                lt_co = ts_buyer["linh_thach"] if ts_buyer else 0
                err_embed = discord.Embed(
                    title="❌ Giao Dịch Thất Bại",
                    description=(
                        f"Không đủ Linh Thạch!\n"
                        f"Cần: **{fmt(self.gia)} LT** — Hiện có: **{fmt(lt_co)} LT**"
                    ),
                    color=0xED4245,
                )
                if self._msg:
                    try: await self._msg.edit(embed=err_embed, view=None)
                    except Exception:
                        log.exception("Lỗi private_trade")
                return

        # Atomic transfer item
        ok, err = await _transfer_item_atomic(
            self.sender_id, self.target_id, self.item, self.so_luong)
        if not ok:
            err_embed = discord.Embed(
                title="❌ Giao Dịch Thất Bại",
                description=f"Lỗi chuyển vật phẩm: {err}\nNgười bán có thể đã dùng/bán item này rồi.",
                color=0xED4245,
            )
            if self._msg:
                try: await self._msg.edit(embed=err_embed, view=None)
                except Exception:
                    log.exception("Lỗi private_trade")
            return

        # Chuyển LT
        if self.gia > 0:
            await add_linh_thach(self.target_id, -self.gia)
            await add_linh_thach(self.sender_id,  self.nhan_ve)

        # Ghi log
        _item_loai = self.item.get("loai", "")
        _item_key  = str(self.item.get("_lq_id", "") or self.item.get("_nl_id", "") or self.item.get("_dtl_key", "") or self.item.get("_dd_id", "") or "")
        await log_giao_dich(
            "private_trade",
            sender_id   = self.sender_id,
            receiver_id = self.target_id,
            item_name   = self.item_name,
            so_luong    = self.so_luong,
            gia_lt      = self.gia,
            ghi_chu     = f"fee={self.phi}lt nhan_ve={self.nhan_ve}lt",
            item_loai   = _item_loai,
            item_key    = _item_key,
        )

        phi_pct = int(TRADE_FEE * 100)
        done_embed = discord.Embed(
            title="✅ Giao Dịch Thành Công!",
            description=(
                f"**{self.dao_hieu_target}** đã chấp nhận giao dịch.\n\n"
                f"📦 **{self.item_name}** × {self.so_luong} → {self.dao_hieu_target}\n"
                + (
                    f"{E_LINH_THACH} **{fmt(self.gia)} LT** → {self.dao_hieu_sender} "
                    f"(sau phí {phi_pct}%: **{fmt(self.nhan_ve)} LT**)"
                    if self.gia > 0 else "🎁 Tặng miễn phí"
                )
            ),
            color=0x57F287,
        )
        if self._msg:
            try: await self._msg.edit(embed=done_embed, view=None)
            except Exception:
                log.exception("Lỗi private_trade")

        # Thông báo cho người bán
        try:
            sender_user = inter.client.get_user(self.sender_id) or \
                          await inter.client.fetch_user(self.sender_id)
            notif_embed = discord.Embed(
                title="✅ Giao Dịch Thành Công!",
                description=(
                    f"**{self.dao_hieu_target}** đã chấp nhận lời mời của bạn.\n\n"
                    f"📦 **{self.item_name}** × {self.so_luong} đã được chuyển.\n"
                    + (
                        f"{E_LINH_THACH} Bạn nhận được: **{fmt(self.nhan_ve)} LT** "
                        f"(phí {phi_pct}%: -{fmt(self.phi)} LT)"
                        if self.gia > 0 else "🎁 Tặng miễn phí — không thu phí."
                    )
                ),
                color=0x57F287,
            )
            await sender_user.send(embed=notif_embed)
        except Exception:
            log.exception("Lỗi private_trade")

    @discord.ui.button(label="❌ Từ Chối", style=discord.ButtonStyle.danger, row=0)
    async def btn_reject(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.target_id:
            return await inter.response.send_message(
                "❌ Lời mời này không dành cho bạn!", ephemeral=True)
        if self._done:
            return await inter.response.send_message(
                "❌ Giao dịch này đã xử lý rồi!", ephemeral=True)

        await inter.response.defer()
        self._done = True
        self.stop()

        reject_embed = discord.Embed(
            title="❌ Lời Mời Bị Từ Chối",
            description=f"**{self.dao_hieu_target}** đã từ chối giao dịch.",
            color=0xED4245,
        )
        if self._msg:
            try: await self._msg.edit(embed=reject_embed, view=None)
            except Exception:
                log.exception("Lỗi private_trade")

        # Thông báo cho người bán
        try:
            sender_user = inter.client.get_user(self.sender_id) or \
                          await inter.client.fetch_user(self.sender_id)
            await sender_user.send(
                embed=discord.Embed(
                    title="❌ Giao Dịch Bị Từ Chối",
                    description=(
                        f"**{self.dao_hieu_target}** đã từ chối lời mời giao dịch:\n"
                        f"**{self.item_name}** × {self.so_luong}"
                    ),
                    color=0xED4245,
                )
            )
        except Exception:
            log.exception("Lỗi private_trade")
