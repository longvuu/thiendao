"""
cogs/views/toa_ky_bi_canh.py
Bí Cảnh Tọa Kỵ — Mount Dungeon System
"""
from __future__ import annotations
from typing import Any
import random
import time

from cogs.views._common import *
from utils.config import (
    TOA_KY, TOA_KY_BY_ID, TOA_KY_LEVEL_MULT,
    TOA_KY_NGUYEN_LIEU, TOA_KY_NL_BY_ID,
    TOA_KY_BI_CANH, TOA_KY_BI_CANH_BY_ID,
    CANH_GIOI, NGUYEN_LIEU,
    fmt, get_cg, bar,
)
from utils.bot_emojis import (
    E_SINH_LUC, E_LINH_LUC, E_CONG_KICH, E_PHONG_NGU,
    E_LINH_THACH, E_TU_VI,
)
from utils.database import get_tu_si, update_tu_si, add_linh_thach, get_the_luc, get_tran_the_luc
from cogs.hoso_utils import (
    _calc_stats, _calc_full_stats, _get_mount_level,
    _bc_sessions, BiCanhSession, _cleanup_stale_sessions,
    _scale_rooms_by_rebirth, SESSION_TIMEOUT_SECS,
    get_the_luc, the_luc_toi_da,
)
import json as _json
import logging

log = logging.getLogger("hoso")


def _embed_toa_ky_bi_canh_chon(ts: dict[str, Any], user) -> discord.Embed:
    """Embed chọn Bí Cảnh Tọa Kỵ."""
    mount_lv = _get_mount_level(ts)
    cg = get_cg(ts["canh_gioi"])
    tl_hien = get_the_luc(ts)
    tl_max = the_luc_toi_da(ts.get("canh_gioi", 0))

    embed = discord.Embed(
        title="🐉 BÍ CẢNH TỌA KỴ",
        description=(
            f"**Mount Level:** {mount_lv}/10\n"
            f"**Thể lực:** {tl_hien}/{tl_max}\n\n"
            "Farm nguyên liệu nâng cấp tọa kỵ tại đây!"
        ),
        color=0xFF6B35)

    for bc in TOA_KY_BI_CANH:
        ok = mount_lv >= bc["cap_toi_thieu"]
        cap_yc = bc["cap_toi_thieu"]
        mark = "✅" if ok else "🔒"
        status = f"Yêu cầu Mount Lv{cap_yc}" if not ok else f"Phí: {bc['the_luc_phi']} thể lực"

        # Show drop info
        nl_names = []
        for nl_id in bc["boss"].get("nl_drop", []):
            nl = TOA_KY_NL_BY_ID.get(nl_id)
            if nl:
                nl_names.append(f"{nl['emoji']}{nl['ten']}")
        drop_str = f" | Drop: {', '.join(nl_names)}" if nl_names else ""

        embed.add_field(
            name=f"{mark} {bc['emoji']} {bc['ten']}",
            value=f"*{bc['mo_ta'][:80]}...*\n{status}{drop_str}",
            inline=False)

    embed.set_footer(text="Chọn bí cảnh từ dropdown bên dưới")
    return embed


class ToaKyBiCanhView(discord.ui.View):
    """View cho Bí Cảnh Tọa Kỵ."""

    def __init__(self, parent, ts: dict[str, Any], actor_id: int = 0, guild_id: int = 0):
        super().__init__(timeout=300)
        self.parent = parent
        self.ts = ts
        self.actor_id = actor_id
        self.guild_id = guild_id
        self._selected_bc_id: int | None = None

        # Dropdown chọn bí cảnh
        mount_lv = _get_mount_level(ts)
        opts = []
        for bc in TOA_KY_BI_CANH:
            ok = mount_lv >= bc["cap_toi_thieu"]
            cap_yc = bc["cap_toi_thieu"]
            label = f"{'✅' if ok else '🔒'} {bc['ten']}"
            desc = f"Yêu cầu: Mount Lv{cap_yc} | Phí: {bc['the_luc_phi']} TL"
            opts.append(discord.SelectOption(
                label=label[:100],
                value=str(bc["id"]),
                emoji=bc["emoji"],
                description=desc[:100]))
        select = discord.ui.Select(
            placeholder="Chọn Bí Cảnh Tọa Kỵ...",
            options=opts,
            row=0)
        self._select = select
        select.callback = self._on_select
        self.add_item(select)

        # Nút vào bí cảnh
        self._btn_vao = discord.ui.Button(
            label="Vào Bí Cảnh", emoji="⚔️",
            style=discord.ButtonStyle.success, row=1, disabled=True)

        async def _noop(inter: discord.Interaction):
            try:
                await inter.response.defer()
            except Exception:
                pass

        self._btn_vao.callback = _noop
        self.add_item(self._btn_vao)

        # Nút quay lại
        btn_back = discord.ui.Button(
            label="◀ Quay Lại", emoji="◀️",
            style=discord.ButtonStyle.secondary, row=2)
        btn_back.callback = self._on_back
        self.add_item(btn_back)

    async def _on_select(self, inter: discord.Interaction):
        if inter.user.id != self.actor_id:
            return await inter.response.send_message("❌", ephemeral=True)

        bc_id = int(inter.data["values"][0])
        if bc_id < 0 or bc_id >= len(TOA_KY_BI_CANH):
            return await inter.response.send_message("❌ Bí cảnh không hợp lệ!", ephemeral=True)

        bc = TOA_KY_BI_CANH[bc_id]
        mount_lv = _get_mount_level(self.ts)

        if mount_lv < bc["cap_toi_thieu"]:
            return await inter.response.send_message(
                embed=e_loi("🔒 Chưa Đủ Level",
                    f"Bí cảnh **{bc['ten']}** yêu cầu **Mount Level {bc['cap_toi_thieu']}**.\n"
                    f"Level hiện tại: **{mount_lv}**"),
                ephemeral=True)

        self._selected_bc_id = bc_id
        boss = bc["boss"]
        tl_hien = get_the_luc(self.ts)

        embed = discord.Embed(
            title=f"🐉 CHUẨN BỊ: {bc['ten']}",
            description=f"Boss trấn giữ: **{boss['emoji']} {boss['ten']}**",
            color=0xFF6B35)

        embed.add_field(
            name="📊 Thông Tin",
            value=(
                f"**Boss HP:** {fmt(boss['hp'])}\n"
                f"**Boss ATK:** {fmt(boss['at'])}\n"
                f"**Phí:** {bc['the_luc_phi']} thể lực\n"
                f"**Thể lực hiện tại:** {tl_hien}"
            ),
            inline=False)

        # Drop info
        nl_lines = []
        for nl_id in bc["boss"].get("nl_drop", []):
            nl = TOA_KY_NL_BY_ID.get(nl_id)
            if nl:
                rate = bc["boss"].get("nl_rate", 0.15)
                nl_lines.append(f"{nl['emoji']} {nl['ten']} ({rate*100:.0f}%)")
        if nl_lines:
            embed.add_field(name="💎 Drop Nguyên Liệu", value="\n".join(nl_lines), inline=False)

        self._btn_vao.disabled = (tl_hien < bc["the_luc_phi"])
        self._btn_vao.callback = self._on_vao

        await inter.response.edit_message(embed=embed, view=self)

    async def _on_vao(self, inter: discord.Interaction):
        if inter.user.id != self.actor_id:
            return await inter.response.send_message("❌", ephemeral=True)

        if self._selected_bc_id is None:
            return await inter.response.send_message("❌ Chưa chọn bí cảnh!", ephemeral=True)

        bc = TOA_KY_BI_CANH[self._selected_bc_id]
        ts_fresh = await get_tu_si(self.actor_id)
        tl = get_the_luc(ts_fresh)

        if tl < bc["the_luc_phi"]:
            return await inter.response.send_message(
                f"❌ **Thể lực không đủ!**\nCần: {bc['the_luc_phi']} | Hiện có: {tl}",
                ephemeral=True)

        # Trừ thể lực
        new_tl = max(0, tl - bc["the_luc_phi"])
        await update_tu_si(self.actor_id, the_luc=new_tl, the_luc_cap_nhat=int(time.time()))

        # Tạo session chiến đấu đơn giản — dùng lại pattern từ bi_canh.py
        #简化: chiến đấu tự động với boss
        ts_after = await get_tu_si(self.actor_id)
        st = _calc_stats(ts_after)
        player_at = st["at"]
        player_hp = ts_after.get("hp_max", 100)

        boss = bc["boss"]
        boss_hp = boss["hp"]
        boss_at = boss["at"]

        # Auto combat: player attacks boss
        player_dmg = max(1, int(player_at * (1 + random.uniform(-0.1, 0.1))))
        boss_turns_to_kill = max(1, (player_hp + boss_at - 1) // boss_at) if boss_at > 0 else 999
        turns_to_kill_boss = max(1, (boss_hp + player_dmg - 1) // player_dmg) if player_dmg > 0 else 999

        player_wins = turns_to_kill_boss <= boss_turns_to_kill

        if player_wins:
            # Thắng! Nhận thưởng
            exp_reward = random.randint(boss["exp_min"], boss["exp_max"])
            lt_reward = random.randint(boss["lt_min"], boss["lt_max"])

            # Drop nguyên liệu
            herb = ts_after.get("toa_ky_herb", {})
            if isinstance(herb, str):
                try: herb = _json.loads(herb) if herb else {}
                except: herb = {}
            if not isinstance(herb, dict):
                herb = {}

            drop_lines = []
            nl_rate = bc["boss"].get("nl_rate", 0.15)
            for nl_id in bc["boss"].get("nl_drop", []):
                if random.random() < nl_rate:
                    herb[nl_id] = herb.get(nl_id, 0) + 1
                    nl = TOA_KY_NL_BY_ID.get(nl_id)
                    if nl:
                        drop_lines.append(f"{nl['emoji']} {nl['ten']} +1")

            await add_linh_thach(self.actor_id, lt_reward)
            await update_tu_si(self.actor_id,
                exp=ts_after.get("exp", 0) + exp_reward,
                toa_ky_herb=herb)

            embed = discord.Embed(
                title=f"🎉 CHIẾN THẮNG! — {bc['ten']}",
                description=f"Đánh bại **{boss['emoji']} {boss['ten']}** trong **{turns_to_kill_boss} lượt**!",
                color=0x57F287)
            embed.add_field(name="📦 Phần Thưởng",
                value=f"**+{fmt(exp_reward)}** {E_TU_VI}  •  **+{fmt(lt_reward)}** {E_LINH_THACH}",
                inline=False)
            if drop_lines:
                embed.add_field(name="💎 Nguyên Liệu Drop",
                    value="\n".join(drop_lines), inline=False)
        else:
            # Thua
            embed = discord.Embed(
                title=f"💀 THẤT BẠI — {bc['ten']}",
                description=(
                    f"**{boss['emoji']} {boss['ten']}** quá mạnh!\n"
                    f"Cần ATK cao hơn hoặc mount level cao hơn."
                ),
                color=0xFF4444)

        embed.set_footer(text=f"Thể lực còn: {new_tl} | Mount Lv{_get_mount_level(ts_after)}")
        await inter.response.send_message(embed=embed, ephemeral=True)

    async def _on_back(self, inter: discord.Interaction):
        if inter.user.id != self.actor_id:
            return await inter.response.send_message("❌", ephemeral=True)
        self.stop()
        try:
            await inter.message.delete()
        except Exception:
            pass
