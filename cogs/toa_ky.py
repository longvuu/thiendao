"""
COG: Tọa Kỵ (Mount System)
Commands: /toaky
"""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import OWNER_IDS
from utils.database import get_tu_si
from utils.embeds import safe_followup

log = logging.getLogger("toa_ky")


class ToaKyCog(commands.Cog):
    """Tọa Kỵ — Mount System"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="toaky", description="Xem và quản lý tọa kỵ (mount)")
    async def toa_ky(self, inter: discord.Interaction):
        ts = await get_tu_si(inter.user.id)
        if not ts:
            return await inter.response.send_message(
                "❌ Bạn chưa có hồ sơ! Dùng `/hoso` để tạo nhân vật.",
                ephemeral=True)

        try:
            from cogs.views.toa_ky import ToaKyView, _embed_toa_ky_list
            embed = _embed_toa_ky_list(ts, inter.user)
            view = ToaKyView(None, ts, inter.user, actor_id=inter.user.id)
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            log.error(f"/toaky user={inter.user.id}: {e}", exc_info=True)
            await inter.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ToaKyCog(bot))
