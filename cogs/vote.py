"""
cogs/vote.py
══════════════════════════════════════════════════════
Hệ thống Biểu Quyết Toàn Server — Owner only.

Lệnh:
    /vote start <câu_hỏi>  → mở vote 24h, gửi tới tất cả world-chat channel
    /vote end               → đóng sớm, công bố kết quả
    /vote status            → xem kết quả tạm thời (ephemeral)

Tính năng:
    - Hai nút ✅ Có / ❌ Không trên mỗi message
    - Cập nhật realtime thanh tiến trình khi có phiếu mới
    - Tự gửi lại message mỗi 30 phút (xóa cũ → gửi mới)
    - Tự đóng và công bố kết quả sau 24 giờ
    - Mỗi user chỉ 1 phiếu; có thể đổi ý
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import OWNER_ID
from utils.database import (
    get_world_chat_channels,
    save_active_vote,
    update_active_vote_data,
    load_active_vote,
    clear_active_vote,
)
from utils.embeds import e_loi, e_ok, e_warn, owner_only_check

log = logging.getLogger("vote")

VOTE_DURATION = 24 * 3600   # 24 giờ
RESEND_EVERY  = 30 * 60     # 30 phút

# ── Singleton vote state (in-memory) ─────────────────────────
_vote: Optional[dict] = None
# Keys:
#   question  : str
#   start     : float  — epoch khi mở
#   end       : float  — epoch hết hạn
#   votes     : dict[int, str]             — user_id → "co" | "khong"
#   messages  : dict[int, tuple[int,int]]  — guild_id → (channel_id, message_id)
#   _bot      : commands.Bot
#   _task     : asyncio.Task


# ══════════════════════════════════════════════════════════════
#  HELPER: BUILD EMBED
# ══════════════════════════════════════════════════════════════

def _build_embed(question: str, votes: dict, end: float, *, closed: bool = False) -> discord.Embed:
    co    = sum(1 for v in votes.values() if v == "co")
    khong = sum(1 for v in votes.values() if v == "khong")
    total = co + khong

    def bar(n: int) -> str:
        if total == 0:
            return "░" * 20 + "  0%"
        pct    = n / total
        filled = round(pct * 20)
        return f"{'█' * filled}{'░' * (20 - filled)}  {pct:.0%}"

    remain  = max(0.0, end - time.time())
    hours   = int(remain) // 3600
    minutes = (int(remain) % 3600) // 60

    color = 0xED4245 if closed else 0x5865F2
    title = "🗳️ KẾT QUẢ BIỂU QUYẾT" if closed else "🗳️ BIỂU QUYẾT TOÀN THIÊN HẠ"

    em = discord.Embed(title=title, description=f"**{question}**\n\u200b", color=color)
    em.add_field(
        name="✅  Có",
        value=f"```{bar(co)}```**{co}** phiếu",
        inline=False,
    )
    em.add_field(
        name="❌  Không",
        value=f"```{bar(khong)}```**{khong}** phiếu",
        inline=False,
    )

    if closed:
        if co > khong:
            verdict = "✅ **Có** thắng!"
        elif khong > co:
            verdict = "❌ **Không** thắng!"
        else:
            verdict = "🤝 **Hòa**!"
        em.add_field(name="Kết Quả", value=verdict, inline=False)
        em.set_footer(text=f"Biểu quyết đã đóng • {total} phiếu tổng cộng")
    else:
        em.set_footer(
            text=f"⏳ Còn {hours}h {minutes}m • {total} phiếu • Mỗi người 1 phiếu, có thể đổi"
        )

    return em


# ══════════════════════════════════════════════════════════════
#  VOTE VIEW (persistent custom_id)
# ══════════════════════════════════════════════════════════════

class VoteView(discord.ui.View):
    """View với 2 nút bỏ phiếu. Dùng custom_id cố định để hoạt động xuyên resend."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅  Có", style=discord.ButtonStyle.success, custom_id="global_vote_co")
    async def btn_co(self, inter: discord.Interaction, button: discord.ui.Button):
        await _handle_vote(inter, "co")

    @discord.ui.button(label="❌  Không", style=discord.ButtonStyle.danger, custom_id="global_vote_khong")
    async def btn_khong(self, inter: discord.Interaction, button: discord.ui.Button):
        await _handle_vote(inter, "khong")


# ══════════════════════════════════════════════════════════════
#  VOTE LOGIC
# ══════════════════════════════════════════════════════════════

async def _handle_vote(inter: discord.Interaction, choice: str):
    """Xử lý khi user bấm nút bỏ phiếu."""
    global _vote

    if _vote is None:
        return await inter.response.send_message(
            embed=e_loi("Không Có Biểu Quyết", "Hiện không có biểu quyết nào đang mở."),
            ephemeral=True,
        )

    if time.time() > _vote["end"]:
        return await inter.response.send_message(
            embed=e_warn("Đã Hết Hạn", "Biểu quyết này đã kết thúc."),
            ephemeral=True,
        )

    uid      = inter.user.id
    previous = _vote["votes"].get(uid)

    if previous == choice:
        label = "✅ Có" if choice == "co" else "❌ Không"
        return await inter.response.send_message(
            embed=e_warn("Đã Bỏ Phiếu Rồi", f"Bạn đã chọn **{label}** rồi!"),
            ephemeral=True,
        )

    _vote["votes"][uid] = choice
    label   = "✅ Có" if choice == "co" else "❌ Không"
    action  = "Đã đổi phiếu sang" if previous else "Đã bỏ phiếu"

    await inter.response.send_message(
        embed=e_ok("Ghi Nhận", f"{action} **{label}**."),
        ephemeral=True,
    )

    # Lưu phiếu vào DB và cập nhật realtime — không chặn response
    asyncio.create_task(_persist_and_refresh())


async def _persist_and_refresh():
    """Lưu state vào DB rồi refresh tất cả message."""
    global _vote
    if _vote is None:
        return
    try:
        # messages dict: key là str (từ JSON) hoặc int — chuẩn hóa thành str để JSON an toàn
        msgs_serializable = {str(k): list(v) for k, v in _vote["messages"].items()}
        votes_serializable = {str(k): v for k, v in _vote["votes"].items()}
        await update_active_vote_data(votes_serializable, msgs_serializable)
    except Exception:
        log.debug("vote: không lưu được DB", exc_info=True)
    await _refresh_all()


async def _refresh_all():
    """Edit tất cả tin nhắn vote đang sống để cập nhật thanh tiến trình."""
    global _vote
    if _vote is None:
        return

    bot   = _vote["_bot"]
    embed = _build_embed(_vote["question"], _vote["votes"], _vote["end"])
    view  = VoteView()

    dead_guilds = []
    for guild_id, (ch_id, msg_id) in list(_vote["messages"].items()):
        try:
            ch  = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            dead_guilds.append(guild_id)
        except Exception:
            log.debug(f"vote: edit thất bại guild {guild_id}", exc_info=True)

    for gid in dead_guilds:
        _vote["messages"].pop(gid, None)


async def _send_to_channels() -> dict[int, tuple[int, int]]:
    """Gửi vote message mới tới tất cả world-chat channel đang active."""
    global _vote
    bot      = _vote["_bot"]
    channels = await get_world_chat_channels()
    messages: dict[int, tuple[int, int]] = {}

    embed = _build_embed(_vote["question"], _vote["votes"], _vote["end"])
    view  = VoteView()

    for row in channels:
        if not row["active"]:
            continue
        try:
            ch  = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
            msg = await ch.send(embed=embed, view=view)
            messages[row["guild_id"]] = (ch.id, msg.id)
        except Exception:
            log.exception(f"vote: không gửi tới guild {row['guild_id']}")

    return messages


async def _delete_current_messages():
    """Xóa tất cả tin nhắn vote hiện tại (chuẩn bị resend)."""
    global _vote
    if _vote is None:
        return

    bot = _vote["_bot"]
    for guild_id, (ch_id, msg_id) in list(_vote["messages"].items()):
        try:
            ch  = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await msg.delete()
        except Exception:
            pass  # Đã xóa / không tìm thấy — bỏ qua


async def _close_vote():
    """Công bố kết quả cuối cùng và reset state."""
    global _vote
    if _vote is None:
        return

    bot   = _vote["_bot"]
    co    = sum(1 for v in _vote["votes"].values() if v == "co")
    khong = sum(1 for v in _vote["votes"].values() if v == "khong")
    total = co + khong

    log.info(f"vote: đóng — '{_vote['question']}' | Có={co} Không={khong} Tổng={total}")

    # Xóa khỏi DB trước
    try:
        await clear_active_vote()
    except Exception:
        log.debug("vote: không xóa được DB", exc_info=True)

    # Xóa tin nhắn cũ
    await _delete_current_messages()

    # Gửi embed kết quả (không có nút)
    embed    = _build_embed(_vote["question"], _vote["votes"], _vote["end"], closed=True)
    channels = await get_world_chat_channels()

    for row in channels:
        if not row["active"]:
            continue
        try:
            ch = bot.get_channel(row["channel_id"]) or await bot.fetch_channel(row["channel_id"])
            await ch.send(embed=embed)
        except Exception:
            log.debug(f"vote: không gửi kết quả tới guild {row['guild_id']}")

    _vote = None


async def _vote_loop():
    """Background task: resend mỗi 30 phút, đóng khi hết 24 giờ."""
    global _vote
    try:
        while True:
            if _vote is None:
                return

            remaining = _vote["end"] - time.time()
            if remaining <= 0:
                break

            # Ngủ đến lần resend tiếp theo hoặc hết giờ
            await asyncio.sleep(min(RESEND_EVERY, remaining))

            if _vote is None:
                return

            # Kiểm tra lại sau khi thức dậy
            if time.time() >= _vote["end"]:
                break

            # Resend: xóa cũ → gửi mới
            await _delete_current_messages()
            _vote["messages"] = await _send_to_channels()
            # Persist messages mới vào DB
            try:
                msgs_s  = {str(k): list(v) for k, v in _vote["messages"].items()}
                votes_s = {str(k): vv for k, vv in _vote["votes"].items()}
                await update_active_vote_data(votes_s, msgs_s)
            except Exception:
                log.debug("vote: không lưu được DB sau resend", exc_info=True)
            log.info("vote: đã gửi lại message (30p)")

        # Hết giờ
        if _vote is not None:
            await _close_vote()

    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("vote: lỗi trong _vote_loop")


# ══════════════════════════════════════════════════════════════
#  COG
# ══════════════════════════════════════════════════════════════

class VoteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Đăng ký persistent view để nút vẫn hoạt động xuyên resend
        bot.add_view(VoteView())

    async def cog_load(self):
        """Khôi phục vote đang mở từ DB sau khi bot restart."""
        asyncio.create_task(self._restore_vote())

    async def _restore_vote(self):
        """Khôi phục _vote từ DB nếu còn trong thời hạn."""
        global _vote
        await self.bot.wait_until_ready()  # chờ bot kết nối xong mới restore
        try:
            row = await load_active_vote()
        except Exception:
            log.debug("vote: không load được DB khi restore", exc_info=True)
            return
        if not row:
            return

        end_ts = row["end_ts"]
        if time.time() >= end_ts:
            # Đã hết hạn trong lúc bot offline — xóa DB và không restore
            try:
                await clear_active_vote()
            except Exception:
                pass
            log.info("vote: vote đã hết hạn khi offline, bỏ qua restore")
            return

        # Khôi phục dữ liệu
        # messages từ DB: key là str guild_id, value là [ch_id, msg_id]
        raw_msgs = row["messages"]
        messages = {int(k): tuple(v) for k, v in raw_msgs.items() if isinstance(v, (list, tuple)) and len(v) == 2}
        # votes: key là str user_id
        votes = {int(k): v for k, v in row["votes"].items()}

        _vote = {
            "question": row["question"],
            "start":    float(row["start_ts"]),
            "end":      float(end_ts),
            "votes":    votes,
            "messages": messages,
            "_bot":     self.bot,
            "_task":    None,
        }

        task           = asyncio.create_task(_vote_loop())
        _vote["_task"] = task
        log.info(f"vote: khôi phục từ DB — '{row['question']}' còn {int(end_ts - time.time())}s")

    vote_group = app_commands.Group(
        name="vote",
        description="Hệ thống biểu quyết toàn server (Owner only)",
    )

    @vote_group.command(name="start", description="[Owner] Mở biểu quyết toàn server trong 24 giờ")
    @owner_only_check(OWNER_ID)
    @app_commands.describe(cau_hoi="Nội dung câu hỏi biểu quyết")
    async def vote_start(self, inter: discord.Interaction, cau_hoi: str):
        global _vote
        await inter.response.defer(ephemeral=True)

        if _vote is not None:
            return await inter.followup.send(
                embed=e_warn(
                    "Đang Có Biểu Quyết",
                    "Đã có biểu quyết đang diễn ra.\nDùng **/vote end** để kết thúc trước.",
                ),
                ephemeral=True,
            )

        now   = time.time()
        _vote = {
            "question": cau_hoi,
            "start":    now,
            "end":      now + VOTE_DURATION,
            "votes":    {},
            "messages": {},
            "_bot":     self.bot,
            "_task":    None,
        }

        _vote["messages"] = await _send_to_channels()
        n = len(_vote["messages"])

        # Lưu vào DB ngay
        try:
            msgs_s  = {str(k): list(v) for k, v in _vote["messages"].items()}
            await save_active_vote(cau_hoi, int(now), int(now + VOTE_DURATION), {}, msgs_s)
        except Exception:
            log.debug("vote: không lưu được DB khi start", exc_info=True)

        task          = asyncio.create_task(_vote_loop())
        _vote["_task"] = task

        log.info(f"vote: bắt đầu — '{cau_hoi}' bởi {inter.user} ({inter.user.id}) | {n} kênh")

        await inter.followup.send(
            embed=e_ok(
                "Đã Mở Biểu Quyết",
                f"**{cau_hoi}**\n\nĐã gửi tới **{n}** kênh.\n"
                f"Biểu quyết tự đóng sau **24 giờ**.",
            ),
            ephemeral=True,
        )

    @vote_group.command(name="end", description="[Owner] Kết thúc biểu quyết sớm và công bố kết quả")
    @owner_only_check(OWNER_ID)
    async def vote_end(self, inter: discord.Interaction):
        global _vote
        await inter.response.defer(ephemeral=True)

        if _vote is None:
            return await inter.followup.send(
                embed=e_loi("Không Có Biểu Quyết", "Hiện không có biểu quyết nào đang mở."),
                ephemeral=True,
            )

        task = _vote.get("_task")
        if task and not task.done():
            task.cancel()

        await _close_vote()

        log.info(f"vote: kết thúc sớm bởi {inter.user} ({inter.user.id})")
        await inter.followup.send(
            embed=e_ok("Đã Đóng", "Biểu quyết đã kết thúc và kết quả đã được gửi."),
            ephemeral=True,
        )

    @vote_group.command(name="status", description="[Owner] Xem kết quả tạm thời của biểu quyết")
    @owner_only_check(OWNER_ID)
    async def vote_status(self, inter: discord.Interaction):
        if _vote is None:
            return await inter.response.send_message(
                embed=e_loi("Không Có Biểu Quyết", "Hiện không có biểu quyết nào đang mở."),
                ephemeral=True,
            )

        embed = _build_embed(_vote["question"], _vote["votes"], _vote["end"])
        await inter.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoteCog(bot))
