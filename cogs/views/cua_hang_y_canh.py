"""
Cửa Hàng Ý Cảnh — Mua Đá Ngộ Đạo và Đá Reset Skill Tree.
Dùng linh thạch.
"""
from __future__ import annotations

import json
import logging

import discord

from utils.config import (
    DA_NGO_DAO_ID, DA_NGO_DAO_GIA,
    DA_RESET_SKILL_TREE_ID, DA_RESET_SKILL_TREE_GIA, fmt,
)
from utils.embeds import e_loi, e_ok, safe_followup

log = logging.getLogger("cua_hang_y_canh")

SHOP_ITEMS = [
    {
        "id": DA_NGO_DAO_ID,
        "ten": "Đá Ngộ Đạo",
        "emoji": "💎",
        "mo_ta": "Dùng để nâng cấp nodes trong Ý Cảnh",
        "gia": DA_NGO_DAO_GIA,
    },
    {
        "id": DA_RESET_SKILL_TREE_ID,
        "ten": "Đá Reset Skill Tree",
        "emoji": "🔄",
        "mo_ta": "Reset toàn bộ Ý Cảnh về Level 0, hoàn điểm",
        "gia": DA_RESET_SKILL_TREE_GIA,
    },
]


def _embed_shop(ts: dict) -> discord.Embed:
    lt = ts.get("linh_thach", 0)
    nl = ts.get("nguyen_lieu", {}) if isinstance(ts.get("nguyen_lieu"), dict) else {}
    embed = discord.Embed(
        title="🏪 CỬA HÀNG Ý CẢNH",
        description=f"Linh thạch hiện có: **{fmt(lt)}** 💎",
        color=0x1ABC9C,
    )
    for item in SHOP_ITEMS:
        so_huu = nl.get(item["id"], 0)
        embed.add_field(
            name=f"{item['emoji']} {item['ten']} — {fmt(item['gia'])} LT",
            value=f"{item['mo_ta']}\nĐang có: **{so_huu}**",
            inline=False,
        )
    return embed


class CuaHangYCanhView(discord.ui.View):
    def __init__(self, parent, ts: dict, actor_id: int = None):
        super().__init__(timeout=120)
        self.parent = parent
        self.ts = ts
        self.actor_id = actor_id or parent.owner_id
        self._build()

    def _build(self):
        self.clear_items()
        for item in SHOP_ITEMS:
            btn = discord.ui.Button(
                label=f"🛒 {item['ten']} — {fmt(item['gia'])} LT",
                style=discord.ButtonStyle.success,
                row=0,
            )
            btn.callback = self._make_buy_cb(item)
            self.add_item(btn)

        btn_back = discord.ui.Button(
            label="◀ Quay lại",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        btn_back.callback = self._on_back
        self.add_item(btn_back)

    def _make_buy_cb(self, item):
        async def cb(inter: discord.Interaction):
            if inter.user.id != self.actor_id:
                return await inter.response.send_message("❌", ephemeral=True)
            view = BuyConfirmView(self, item, self.actor_id)
            embed = discord.Embed(
                title=f"🛒 Mua {item['ten']}",
                description=(
                    f"**{item['ten']}** — {fmt(item['gia'])} LT mỗi viên\n\n"
                    f"Bạn muốn mua bao nhiêu?\n(Linh thạch hiện có: **{fmt(self.ts.get('linh_thach', 0))}**)"
                ),
                color=0x1ABC9C,
            )
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        return cb

    async def _on_back(self, inter: discord.Interaction):
        from cogs.hoso_utils import _back_to_hoso
        await _back_to_hoso(inter, self.parent)


class BuyConfirmView(discord.ui.View):
    def __init__(self, parent_view, item, actor_id):
        super().__init__(timeout=30)
        self.parent_view = parent_view
        self.item = item
        self.actor_id = actor_id

    @discord.ui.button(label="×1", style=discord.ButtonStyle.success, row=0)
    async def buy_1(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._buy(inter, 1)

    @discord.ui.button(label="×5", style=discord.ButtonStyle.success, row=0)
    async def buy_5(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._buy(inter, 5)

    @discord.ui.button(label="×10", style=discord.ButtonStyle.primary, row=0)
    async def buy_10(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._buy(inter, 10)

    @discord.ui.button(label="❌ Hủy", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        self.stop()
        await inter.response.edit_message(
            embed=e_ok("Đã Hủy", "Không mua gì."), view=self)

    async def _buy(self, inter: discord.Interaction, qty: int):
        if inter.user.id != self.actor_id:
            return await inter.response.send_message("❌", ephemeral=True)
        await inter.response.defer(ephemeral=True)

        from utils.database import get_tu_si, update_tu_si
        ts = await get_tu_si(self.actor_id)
        if not ts:
            self.stop()
            return await safe_followup(inter, "❌ Không tìm thấy hồ sơ!", ephemeral=True)

        lt = ts.get("linh_thach", 0)
        total_cost = self.item["gia"] * qty
        if lt < total_cost:
            return await safe_followup(inter,
                f"❌ Không đủ linh thạch! Cần **{fmt(total_cost)}**, có **{fmt(lt)}**.",
                ephemeral=True)

        nl = ts.get("nguyen_lieu", {}) if isinstance(ts.get("nguyen_lieu"), dict) else {}
        nl[self.item["id"]] = nl.get(self.item["id"], 0) + qty

        await update_tu_si(self.actor_id, linh_thach=lt - total_cost, nguyen_lieu=nl)

        for item in self.children:
            item.disabled = True
        self.stop()

        await safe_followup(inter,
            embed=e_ok("Mua Thành Công",
                f"Đã mua **{qty}× {self.item['emoji']} {self.item['ten']}**\n"
                f"Trừ **{fmt(total_cost)}** Linh Thạch."),
            ephemeral=True)
