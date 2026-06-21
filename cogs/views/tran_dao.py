"""
Trận Đạo (陣道) — Choose 1 Active Formation
Mở sau lần trùng sinh đầu tiên.
Trận đầu tiên (Liệt Hỏa) mặc định mở khóa.
Trận khác mở khi unlock node tương ứng trong Ý Cảnh.
"""
from __future__ import annotations

import json
import logging

import discord

from utils.config import TRAN_DAO, TRAN_DAO_BY_ID, Y_CANH_ALL_NODES, fmt
from utils.embeds import e_ok, e_warn, safe_followup

log = logging.getLogger("tran_dao")


def _is_unlocked(ts: dict, unlock_cfg: dict) -> bool:
    """Kiểm tra trận đã unlock chưa dựa trên Ý Cảnh."""
    if not unlock_cfg:
        return True
    from cogs.views.y_canh import _get_y_canh
    y_canh = _get_y_canh(ts)
    node_id = unlock_cfg["node"]
    return y_canh.get(node_id, 0) >= 1


def _embed_tran_dao(ts: dict) -> discord.Embed:
    active = ts.get("tran_dao_active", "")
    so_lan_ts = ts.get("so_lan_trung_sinh", 0)

    embed = discord.Embed(
        title="⚔️ TRẬN ĐẠO",
        description=(
            "Chọn 1 trận đạo để kích hoạt. Mỗi trận buff khác nhau.\n"
            f"Trận đang active: **{TRAN_DAO_BY_ID[active]['ten'] if active and active in TRAN_DAO_BY_ID else 'Không có'}**"
        ),
        color=0xE74C3C,
    )

    for t in TRAN_DAO:
        unlocked = _is_unlocked(ts, t["unlock"])
        is_active = t["id"] == active

        buff_str = ", ".join(f"+{v}% {k}" for k, v in t["buff"].items())
        debuff_str = ", ".join(f"{v}% {k}" for k, v in t["debuff"].items())

        status = "🟢 **ACTIVE**" if is_active else ("✅ Đã mở" if unlocked else "🔒 Chưa mở")
        unlock_text = ""
        if not unlocked and t["unlock"]:
            nd = Y_CANH_ALL_NODES.get(t["unlock"]["node"], {})
            unlock_text = f"\n*Mở: Nâng cấp **{nd.get('ten', t['unlock']['node'])}** trong Ý Cảnh*"

        embed.add_field(
            name=f"{t['emoji']} {t['ten']} — {status}",
            value=f"{t['mo_ta']}\n**BUFF:** {buff_str}\n**DEBUFF:** {debuff_str}{unlock_text}",
            inline=False,
        )

    return embed


class TranDaoView(discord.ui.View):
    def __init__(self, parent, ts: dict, actor_id: int = None):
        super().__init__(timeout=120)
        self.parent = parent
        self.ts = ts
        self.actor_id = actor_id or parent.owner_id
        self._build()

    def _build(self):
        self.clear_items()
        for t in TRAN_DAO:
            unlocked = _is_unlocked(self.ts, t["unlock"])
            is_active = self.ts.get("tran_dao_active", "") == t["id"]
            btn = discord.ui.Button(
                label=f"{t['emoji']} {t['ten']}",
                style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary,
                disabled=is_active or not unlocked,
                row=0,
            )
            btn.callback = self._make_activate_cb(t)
            self.add_item(btn)

        btn_back = discord.ui.Button(
            label="◀ Quay lại",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        btn_back.callback = self._on_back
        self.add_item(btn_back)

    def _make_activate_cb(self, t):
        async def cb(inter: discord.Interaction):
            if inter.user.id != self.actor_id:
                return await inter.response.send_message("❌", ephemeral=True)
            await inter.response.defer(ephemeral=True)

            from utils.database import update_tu_si
            await update_tu_si(self.actor_id, tran_dao_active=t["id"])

            self.ts["tran_dao_active"] = t["id"]
            embed = _embed_tran_dao(self.ts)
            view = TranDaoView(self.parent, self.ts, self.actor_id)
            await safe_followup(inter,
                embed=e_ok("Kích Hoạt", f"✅ Đã chọn **{t['ten']}**!"),
                ephemeral=True)
        return cb

    async def _on_back(self, inter: discord.Interaction):
        from cogs.hoso_utils import _back_to_hoso
        await _back_to_hoso(inter, self.parent)
