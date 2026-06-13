"""
cogs/world_chat.py
══════════════════════════════════════════════════════
Kênh Thế Giới — Chat liên server realtime + Hồ sơ & PK liên server.

Flow world chat:
  1. Admin dùng /worldchat setup → tạo webhook, lưu DB
  2. on_message: bắt tin nhắn user thật (bỏ qua webhook/bot), forward song song
     - Bỏ qua message.webhook_id để không re-forward tin từ server khác
     - React ⏳ nếu cooldown, không react gì khi forward thành công
  3. /worldchat disable → tắt, xóa webhook
  4. /worldchat status → xem danh sách server

Flow hồ sơ liên server:
  - /worldchat hoso <user_id> → tra hồ sơ bất kỳ (không banner)

Flow PK liên server:
  - /worldchat pk <user_id> [cuoc] → gửi lời thách, broadcast qua tất cả world chat
  - /worldchat pk-nhan → target chấp nhận, stream combat từng chunk qua webhook
  - /worldchat pk-tu  → từ chối
  - /worldchat pk-huy → huỷ lời thách đã gửi

Lưu ý kỹ thuật:
  - Webhook không thể edit message ở server khác → stream bằng nhiều message
  - _broadcast_cross_pvp gửi tới TẤT CẢ server (cả server gốc)
  - _broadcast thông thường chỉ gửi tới server KHÁC
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.database import (
    get_world_chat_channels,
    set_world_chat_channel,
    disable_world_chat,
    get_world_chat_by_guild,
    mark_webhook_inactive,
    get_tu_si,
    create_cross_challenge,
    get_pending_cross_challenge,
    get_pending_by_challenger,
    resolve_cross_challenge,
    expire_old_cross_challenges,
)
from utils.embeds import e_loi, e_ok, e_warn, safe_followup

log = logging.getLogger("world_chat")

# ── In-memory cooldown (world chat) ───────────────────────────
_user_cd: dict[int, float] = {}
COOLDOWN_SECS   = 2.0
MAX_LEN         = 1000
INVITE_PATTERN  = re.compile(r"discord\.gg/|discord\.com/invite/", re.IGNORECASE)
WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=8)

# ── In-memory cooldown (cross PvP) ────────────────────────────
_xpvp_cd: dict[int, float] = {}
XPVP_CD_SECS = 300   # 5 phút

# ── Cross PvP game constants (khớp pvp.py) ────────────────────
MAX_CG_DIFF  = 2
BASE_EXP_WIN = 300
BASE_LT_WIN  = 500
MAX_CUOC     = 10_000_000  # khớp pvp.py


# ══════════════════════════════════════════════════════════════
#  FORWARD ENGINE
# ══════════════════════════════════════════════════════════════
async def _forward_message(
    session: aiohttp.ClientSession,
    webhook_url: str,
    username: str,
    avatar_url: str | None,
    content: str,
    guild_id: int,
    thread_id: int | None = None,
) -> bool:
    """
    Gửi 1 message tới 1 webhook.
    Trả về True nếu thành công, False nếu webhook dead (404/401/403).
    """
    payload: dict = {
        "username":        username[:80],
        "content":         content[:2000],
        "allowed_mentions": {"parse": []},
    }
    if avatar_url:
        payload["avatar_url"] = avatar_url

    post_url = webhook_url
    if thread_id:
        post_url = f"{webhook_url}?thread_id={thread_id}"

    try:
        async with session.post(post_url, json=payload, timeout=WEBHOOK_TIMEOUT) as resp:
            if resp.status in (200, 204):
                return True
            if resp.status in (404, 401, 403):
                log.warning(f"WorldChat: webhook dead ({resp.status}) guild={guild_id}")
                return False
            if resp.status == 429:
                log.warning(f"WorldChat: rate limited guild={guild_id}")
                return True
            log.warning(f"WorldChat: unexpected status={resp.status} guild={guild_id}")
            return True
    except asyncio.TimeoutError:
        log.warning(f"WorldChat: timeout guild={guild_id}")
        return True   # timeout ≠ dead
    except Exception as e:
        log.error(f"WorldChat: forward error guild={guild_id}: {e}")
        return True


async def _broadcast(
    bot: commands.Bot,
    sender_guild_id: int,
    username: str,
    avatar_url: str | None,
    content: str,
) -> int:
    """
    Forward tới tất cả server đang active TRỪ server gửi.
    Trả về số server nhận thành công.
    """
    channels = await get_world_chat_channels()
    targets  = [c for c in channels if c["guild_id"] != sender_guild_id and c["active"]]
    if not targets:
        return 0

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[
            _forward_message(session, c["webhook_url"], username, avatar_url,
                             content, c["guild_id"], thread_id=c.get("thread_id"))
            for c in targets
        ], return_exceptions=True)

    ok_count = 0
    for c, ok in zip(targets, results):
        if ok is False:
            await mark_webhook_inactive(c["guild_id"])
            log.info(f"WorldChat: marked guild={c['guild_id']} inactive")
        elif ok is True:
            ok_count += 1
    return ok_count


async def _broadcast_cross_pvp(bot: commands.Bot, content: str) -> None:
    """
    Gửi thông báo PvP liên server tới TẤT CẢ server đang active
    (kể cả server gốc — target có thể ở bất kỳ đâu).
    """
    channels = await get_world_chat_channels()
    targets  = [c for c in channels if c["active"]]
    if not targets:
        return

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[
            _forward_message(session, c["webhook_url"], "⚔️ Hệ Thống Thế Giới",
                             None, content, c["guild_id"], thread_id=c.get("thread_id"))
            for c in targets
        ], return_exceptions=True)

    for c, ok in zip(targets, results):
        if ok is False:
            await mark_webhook_inactive(c["guild_id"])


# ══════════════════════════════════════════════════════════════
#  CROSS-PvP COMBAT RUNNER
# ══════════════════════════════════════════════════════════════
async def _run_cross_combat(
    bot: commands.Bot,
    challenge: dict,
    ts_c: dict,
    ts_t: dict,
    challenger_name: str,
    target_name: str,
) -> None:
    """Tính combat rồi broadcast 1 message kết quả qua tất cả world chat."""
    from cogs.views.pvp import _compute_pvp
    from cogs.views._common import get_cg_ten, fmt as _fmt, add_linh_thach, update_tu_si

    cid  = challenge["challenger_id"]
    tid  = challenge["target_id"]
    cuoc = challenge["cuoc_lt"]

    loop = asyncio.get_running_loop()
    try:
        logs, hp_max_a, hp_max_b = await loop.run_in_executor(
            None, _compute_pvp, ts_c, ts_t)
    except Exception as e:
        log.error(f"CrossPvP compute error: {e}", exc_info=True)
        await _broadcast_cross_pvp(bot,
            f"❌ Lỗi tính toán PK liên server (mã #{challenge['id']}): {e}")
        return

    hp_a = logs[-1][7] if logs else hp_max_a
    hp_b = logs[-1][8] if logs else hp_max_b

    if   hp_a > 0 and hp_b <= 0: winner_id, loser_id = cid, tid
    elif hp_b > 0 and hp_a <= 0: winner_id, loser_id = tid, cid
    elif hp_a > hp_b:             winner_id, loser_id = cid, tid
    elif hp_b > hp_a:             winner_id, loser_id = tid, cid
    else:                         winner_id = loser_id = None

    lt_won = exp_won = 0
    if winner_id:
        w_ts    = ts_c if winner_id == cid else ts_t
        cg_w    = w_ts["canh_gioi"]
        exp_won = int(BASE_EXP_WIN * (1 + cg_w * 0.1))
        lt_won  = int(BASE_LT_WIN  * (1 + cg_w * 0.1))
        if cuoc > 0:
            lt_won += cuoc

    # DB update
    try:
        if winner_id and loser_id:
            w_ts = ts_c if winner_id == cid else ts_t
            l_ts = ts_t if winner_id == cid else ts_c
            await add_linh_thach(winner_id, lt_won)
            await update_tu_si(winner_id,
                exp=w_ts["exp"] + exp_won,
                thang_pvp=w_ts.get("thang_pvp", 0) + 1)
            await update_tu_si(loser_id,
                thua_pvp=l_ts.get("thua_pvp", 0) + 1)
            if cuoc > 0:
                await add_linh_thach(loser_id, -cuoc)
    except Exception as e:
        log.error(f"CrossPvP DB error: {e}", exc_info=True)

    _xpvp_cd[cid] = time.monotonic()
    _xpvp_cd[tid] = time.monotonic()
    if len(_xpvp_cd) > 500:
        cutoff = time.monotonic() - XPVP_CD_SECS * 2
        for u in [x for x, t in list(_xpvp_cd.items()) if t < cutoff]:
            _xpvp_cd.pop(u, None)

    cg_c       = get_cg_ten(ts_c["canh_gioi"], ts_c["cap_nho"])
    cg_t       = get_cg_ten(ts_t["canh_gioi"], ts_t["cap_nho"])
    total_hiep = len(logs)

    # 5 hiệp cuối
    show     = logs[-5:] if len(logs) > 5 else logs
    log_text = "\n".join(l[9] for l in show)
    if len(logs) > 5:
        log_text = f"*(… bỏ qua {len(logs)-5} hiệp đầu)*\n" + log_text

    hp_pct_a = int(hp_a / hp_max_a * 100) if hp_max_a else 0
    hp_pct_b = int(hp_b / hp_max_b * 100) if hp_max_b else 0

    if winner_id:
        w_name = challenger_name if winner_id == cid else target_name
        l_name = target_name    if winner_id == cid else challenger_name
        result = f"🏆 **{w_name}** ĐẠI THẮNG!"
        reward = f"🎖️ +{_fmt(lt_won)} LT · +{_fmt(exp_won)} Tu Vi"
        if cuoc > 0:
            reward += f" · 💸 {l_name} mất {_fmt(cuoc)} LT cược"
    else:
        result = "⚖️ **HÒA — Cân Sức!**"
        reward = "Không trao thưởng."

    content = (
        f"⚔️ **KẾT QUẢ PK LIÊN SERVER** ⚔️\n"
        f"🗡️ **{challenger_name}** `{cg_c}` vs **{target_name}** `{cg_t}`\n"
        f"❤️ HP: {challenger_name} {hp_pct_a}% · {target_name} {hp_pct_b}%\n"
        f"📜 **{total_hiep} hiệp** — Diễn biến cuối:\n{log_text}\n"
        f"{result}\n{reward}"
    )
    # Cắt nếu quá 2000 ký tự
    if len(content) > 1950:
        content = content[:1950] + "\n…"
    await _broadcast_cross_pvp(bot, content)


# ══════════════════════════════════════════════════════════════
#  COG
# ══════════════════════════════════════════════════════════════
class WorldChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── on_message ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Bỏ qua webhook (tin forward từ server khác), bot, DM
        if message.webhook_id or message.author.bot or not message.guild:
            return

        record = await get_world_chat_by_guild(message.guild.id)
        if not record or not record["active"]:
            return
        if message.channel.id != record["channel_id"]:
            return

        author = message.author

        # Cooldown
        now  = time.monotonic()
        last = _user_cd.get(author.id, 0)
        if now - last < COOLDOWN_SECS:
            try:
                await message.add_reaction("⏳")
            except Exception:
                log.exception("Lỗi world_chat")
            return
        _user_cd[author.id] = now
        # Dọn entries cũ tránh memory leak
        if len(_user_cd) > 1000:
            cutoff = now - 300
            for u in [x for x, t in list(_user_cd.items()) if t < cutoff]:
                _user_cd.pop(u, None)

        content = message.content or ""

        # Block invite link
        if INVITE_PATTERN.search(content):
            try:
                await message.delete()
                await message.channel.send(
                    f"❌ {author.mention} Không được gửi link mời server trong kênh thế giới!",
                    delete_after=5)
            except Exception:
                log.exception("Lỗi world_chat")
            return

        # Kèm attachment URL nếu có
        if not content and message.attachments:
            content = " ".join(a.url for a in message.attachments[:3])
        elif message.attachments:
            content += "\n" + " ".join(a.url for a in message.attachments[:3])

        if not content.strip():
            return

        if len(content) > MAX_LEN:
            content = content[:MAX_LEN] + "…"

        server_name = message.guild.name[:20]
        username    = f"[{server_name}] {author.display_name}"
        avatar_url  = str(author.display_avatar.url) if author.display_avatar else None

        # Forward — await trực tiếp (gather song song bên trong)
        # Không react gì khi thành công — chỉ react ⏳ khi cooldown (ở trên)
        await _broadcast(self.bot, message.guild.id, username, avatar_url, content)

    # ── /worldchat group ───────────────────────────────────────
    worldchat_group = app_commands.Group(
        name="worldchat",
        description="Kênh thế giới — chat, hồ sơ & PK liên server",
    )

    # ── setup ─────────────────────────────────────────────────
    @worldchat_group.command(
        name="setup",
        description="Đăng ký kênh thế giới — gõ lệnh trong channel muốn dùng",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_setup(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)

        current   = inter.channel
        thread_id: int | None = None

        if isinstance(current, discord.Thread):
            thread_id       = current.id
            webhook_channel = current.parent
            display_ch      = current
            if webhook_channel is None:
                return await safe_followup(inter,
                    embed=e_loi("❌ Lỗi", "Không tìm thấy channel cha của thread."),
                    ephemeral=True)
        elif isinstance(current, discord.TextChannel):
            webhook_channel = current
            display_ch      = current
        else:
            return await safe_followup(inter,
                embed=e_loi("❌ Không Hỗ Trợ",
                    "Gõ lệnh trong **text channel** hoặc **thread** muốn dùng."),
                ephemeral=True)

        me = inter.guild.me
        if not webhook_channel.permissions_for(me).manage_webhooks:
            return await safe_followup(inter,
                embed=e_loi("❌ Thiếu Quyền",
                    f"Bot cần **Manage Webhooks** trong {webhook_channel.mention}."),
                ephemeral=True)
        if not webhook_channel.permissions_for(me).send_messages:
            return await safe_followup(inter,
                embed=e_loi("❌ Thiếu Quyền",
                    f"Bot cần **Send Messages** trong {webhook_channel.mention}."),
                ephemeral=True)

        # Xóa webhook cũ nếu có
        existing = await get_world_chat_by_guild(inter.guild.id)
        if existing and existing.get("webhook_url"):
            try:
                old_id = _extract_webhook_id(existing["webhook_url"])
                if old_id:
                    old_wh = await self.bot.fetch_webhook(old_id)
                    await old_wh.delete(reason="WorldChat reconfigure")
            except Exception:
                log.exception("Lỗi world_chat")

        try:
            webhook = await webhook_channel.create_webhook(
                name="🌐 Kênh Thế Giới",
                reason=f"WorldChat setup by {inter.user}",
            )
        except discord.Forbidden:
            return await safe_followup(inter,
                embed=e_loi("❌ Forbidden", "Bot không có quyền tạo webhook."),
                ephemeral=True)
        except Exception as e:
            return await safe_followup(inter,
                embed=e_loi("❌ Lỗi", f"Không tạo được webhook: {e}"),
                ephemeral=True)

        await set_world_chat_channel(
            inter.guild.id, display_ch.id, webhook.url, thread_id=thread_id)

        all_ch      = await get_world_chat_channels()
        active_cnt  = sum(1 for c in all_ch if c["active"])
        loc_str     = f"thread **{display_ch.name}**" if thread_id else display_ch.mention

        await safe_followup(inter,
            embed=e_ok("✅ Kênh Thế Giới Đã Bật",
                f"{display_ch.mention} đã được đăng ký.\n\n"
                f"🌐 Hiện có **{active_cnt}** server đang kết nối.\n"
                f"Tin nhắn trong {loc_str} sẽ được forward tới tất cả server khác.\n\n"
                f"**Lưu ý:** Không gửi link mời · Cooldown 2s · Tối đa {MAX_LEN} ký tự"),
            ephemeral=True)

        try:
            await display_ch.send(embed=discord.Embed(
                title="🌐 Kênh Thế Giới Đã Kích Hoạt",
                description=(
                    f"Đây đã được kết nối với **{active_cnt}** server khác.\n"
                    "Mọi tin nhắn tại đây sẽ được chia sẻ toàn cầu! 🌏"
                ),
                color=0x3498DB,
            ))
        except Exception:
            log.exception("Lỗi world_chat")

        log.info(f"WorldChat: setup guild={inter.guild.id} ch={display_ch.id} thread={thread_id}")

    # ── disable ───────────────────────────────────────────────
    @worldchat_group.command(name="disable", description="Tắt kênh thế giới cho server này")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_disable(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)

        existing = await get_world_chat_by_guild(inter.guild.id)
        if not existing or not existing["active"]:
            return await safe_followup(inter,
                embed=e_warn("⚠️ Chưa Bật", "Server này chưa đăng ký kênh thế giới."),
                ephemeral=True)

        try:
            wh_id = _extract_webhook_id(existing["webhook_url"])
            if wh_id:
                wh = await self.bot.fetch_webhook(wh_id)
                await wh.delete(reason="WorldChat disabled")
        except Exception:
            log.exception("Lỗi world_chat")

        await disable_world_chat(inter.guild.id)
        await safe_followup(inter,
            embed=e_ok("✅ Đã Tắt", "Kênh thế giới đã bị tắt."),
            ephemeral=True)
        log.info(f"WorldChat: disabled guild={inter.guild.id}")

    # ── status ────────────────────────────────────────────────
    @worldchat_group.command(name="status", description="Xem danh sách server đang kết nối")
    async def cmd_status(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)

        channels = await get_world_chat_channels()
        active   = [c for c in channels if c["active"]]

        if not active:
            return await safe_followup(inter,
                embed=e_warn("🌐 Kênh Thế Giới", "Chưa có server nào đăng ký."),
                ephemeral=True)

        lines = []
        for c in active:
            guild = self.bot.get_guild(c["guild_id"])
            name  = guild.name if guild else f"Server {c['guild_id']}"
            this  = " *(server này)*" if c["guild_id"] == inter.guild.id else ""
            lines.append(f"🌐 **{name}**{this}")

        embed = discord.Embed(
            title=f"🌐 Kênh Thế Giới — {len(active)} server",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text="Tin nhắn được forward realtime giữa tất cả server")
        await safe_followup(inter, embed=embed, ephemeral=True)

    # ── hoso ──────────────────────────────────────────────────
    @worldchat_group.command(
        name="hoso",
        description="Tra cứu hồ sơ tu sĩ bất kỳ theo User ID (liên server)",
    )
    @app_commands.describe(user_id="Discord User ID (chuột phải → Sao chép ID)")
    async def cmd_hoso(self, inter: discord.Interaction, user_id: str):
        await inter.response.defer(ephemeral=True)

        try:
            uid = int(user_id.strip())
        except ValueError:
            return await safe_followup(inter,
                embed=e_loi("❌ Sai Định Dạng",
                    "User ID phải là dãy số. Chuột phải vào tên → **Sao chép ID**."),
                ephemeral=True)

        ts = await get_tu_si(uid)
        if not ts:
            return await safe_followup(inter,
                embed=e_warn("🔍 Không Tìm Thấy",
                    f"Không có tu sĩ nào với ID `{uid}` trong hệ thống."),
                ephemeral=True)

        # Lấy discord.User để có avatar
        try:
            discord_user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
        except Exception:
            discord_user = None

        if discord_user is None:
            class _FakeUser:
                display_name = ts.get("dao_hieu", str(uid))
                class _Av:
                    url = "https://cdn.discordapp.com/embed/avatars/0.png"
                display_avatar = _Av()
            discord_user = _FakeUser()

        from cogs.hoso_utils import _embed_hoso
        embed = _embed_hoso(ts, discord_user, is_own=False)

        # Footer ghi rõ server nếu bot chung guild
        mutual = next((g for g in self.bot.guilds if g.get_member(uid)), None)
        server_tag = f" · {mutual.name}" if mutual else ""
        embed.set_footer(text=f"🌐 Hồ Sơ Liên Server — ID: {uid}{server_tag}")

        await safe_followup(inter, embed=embed, ephemeral=True)

    # ── pk ────────────────────────────────────────────────────
    @worldchat_group.command(
        name="pk",
        description="Thách đấu tu sĩ bất kỳ liên server theo User ID",
    )
    @app_commands.describe(
        user_id="Discord User ID của tu sĩ muốn thách (chuột phải → Sao chép ID)",
        cuoc="Số Linh Thạch muốn cược (mặc định 0)",
    )
    async def cmd_pk(self, inter: discord.Interaction, user_id: str, cuoc: int = 0):
        await inter.response.defer(ephemeral=True)

        try:
            tid = int(user_id.strip())
        except ValueError:
            return await safe_followup(inter,
                embed=e_loi("❌ Sai Định Dạng", "User ID phải là dãy số."),
                ephemeral=True)

        try:
            cid = inter.user.id
            if tid == cid:
                return await safe_followup(inter,
                    embed=e_loi("❌ Lỗi", "Không thể tự thách mình!"),
                    ephemeral=True)

            await expire_old_cross_challenges()

            # Cooldown
            now    = time.monotonic()
            remain = XPVP_CD_SECS - (now - _xpvp_cd.get(cid, 0))
            if remain > 0:
                m, s = int(remain) // 60, int(remain) % 60
                return await safe_followup(inter,
                    embed=e_warn("⏳ Đang Hồi Sức",
                        f"Cần nghỉ thêm **{m}p {s}s** trước khi thách đấu tiếp."),
                    ephemeral=True)

            # Chặn spam lời thách
            existing = await get_pending_by_challenger(cid)
            if existing:
                return await safe_followup(inter,
                    embed=e_warn("⏳ Đang Chờ",
                        "Bạn đang có lời thách chưa được trả lời.\n"
                        "Dùng `/worldchat pk-huy` để huỷ."),
                    ephemeral=True)

            ts_c = await get_tu_si(cid)
            ts_t = await get_tu_si(tid)

            if not ts_c:
                return await safe_followup(inter,
                    embed=e_loi("❌ Chưa Tu Tiên", "Dùng **/hoso** để tạo hồ sơ trước."),
                    ephemeral=True)
            if not ts_t:
                return await safe_followup(inter,
                    embed=e_loi("❌ Không Tìm Thấy",
                        f"Tu sĩ ID `{tid}` chưa có hồ sơ trong hệ thống."),
                    ephemeral=True)

            diff = abs(ts_c["canh_gioi"] - ts_t["canh_gioi"])
            if diff > MAX_CG_DIFF:
                return await safe_followup(inter,
                    embed=e_loi("❌ Chênh Lệch Quá Lớn",
                        f"Chênh nhau **{diff} cảnh giới** (tối đa {MAX_CG_DIFF})."),
                    ephemeral=True)

            cuoc = max(0, cuoc)
            if cuoc > MAX_CUOC:
                return await safe_followup(inter,
                    embed=e_warn("⚠️ Cược Quá Lớn",
                        f"Cược tối đa là **{MAX_CUOC:,} LT** để tránh giao dịch bất chính."),
                    ephemeral=True)
            if cuoc > 0:
                if ts_c["linh_thach"] < cuoc:
                    return await safe_followup(inter,
                        embed=e_loi("❌ Không Đủ LT",
                            f"Bạn chỉ có **{ts_c['linh_thach']:,}** LT."),
                        ephemeral=True)
                if ts_t["linh_thach"] < cuoc:
                    return await safe_followup(inter,
                        embed=e_warn("⚠️ Mục Tiêu Không Đủ LT",
                            f"Tu sĩ ID `{tid}` chỉ có **{ts_t['linh_thach']:,}** LT, "
                            "không đủ đáp cược."),
                        ephemeral=True)

            challenge_id = await create_cross_challenge(cid, inter.guild.id, tid, cuoc)

            try:
                target_user = self.bot.get_user(tid) or await self.bot.fetch_user(tid)
                target_name = target_user.display_name
            except Exception:
                target_name = ts_t.get("dao_hieu", str(tid))

            from cogs.views._common import get_cg_ten
            cg_c     = get_cg_ten(ts_c["canh_gioi"], ts_c["cap_nho"])
            cg_t     = get_cg_ten(ts_t["canh_gioi"], ts_t["cap_nho"])
            cuoc_str = f"**{cuoc:,} LT**" if cuoc > 0 else "*(không cược)*"

            notif = (
                f"⚔️ **THÁCH ĐẤU LIÊN SERVER** ⚔️\n"
                f"🗡️ **{inter.user.display_name}** `{cg_c}` (ID: `{cid}`)\n"
                f"thách đấu **{target_name}** `{cg_t}` (ID: `{tid}`)\n"
                f"💰 Cược: {cuoc_str}\n"
                f"📋 Mã thách: `#{challenge_id}`\n"
                f"⏳ **{target_name}** dùng `/worldchat pk-nhan` để chấp nhận "
                f"hoặc `/worldchat pk-tu` để từ chối *(hết hạn sau 5 phút)*"
            )
            asyncio.create_task(_broadcast_cross_pvp(self.bot, notif))

            await safe_followup(inter,
                embed=e_ok("✅ Đã Gửi Lời Thách",
                    f"Lời thách **#{challenge_id}** đã được phát trên tất cả kênh thế giới!\n\n"
                    f"🎯 Mục tiêu: **{target_name}** (ID: `{tid}`)\n"
                    f"💰 Cược: {cuoc_str}\n"
                    f"⏳ Hết hạn sau **5 phút** nếu không có phản hồi."),
                ephemeral=True)

        except Exception as e:
            log.error(f"cmd_pk error user={inter.user.id}: {e}", exc_info=True)
            await safe_followup(inter,
                embed=e_loi("❌ Lỗi", f"Có lỗi xảy ra: {e}"),
                ephemeral=True)

    # ── pk-nhan ───────────────────────────────────────────────
    @worldchat_group.command(
        name="pk-nhan",
        description="Chấp nhận lời thách đấu liên server",
    )
    async def cmd_pk_nhan(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        try:
            await expire_old_cross_challenges()

            uid       = inter.user.id
            challenge = await get_pending_cross_challenge(uid)
            if not challenge:
                return await safe_followup(inter,
                    embed=e_warn("❌ Không Có Lời Thách",
                        "Bạn không có lời thách đấu liên server nào đang chờ."),
                    ephemeral=True)

            # Lock ngay để tránh double-accept
            await resolve_cross_challenge(challenge["id"], "accepted")

            cid  = challenge["challenger_id"]
            cuoc = challenge["cuoc_lt"]

            ts_c = await get_tu_si(cid)
            ts_t = await get_tu_si(uid)

            if not ts_c or not ts_t:
                await resolve_cross_challenge(challenge["id"], "expired")
                return await safe_followup(inter,
                    embed=e_loi("❌ Lỗi Dữ Liệu",
                        "Không tìm thấy hồ sơ một trong hai tu sĩ."),
                    ephemeral=True)

            # Validate lại cược (LT có thể thay đổi)
            if cuoc > 0 and (ts_c["linh_thach"] < cuoc or ts_t["linh_thach"] < cuoc):
                await resolve_cross_challenge(challenge["id"], "expired")
                return await safe_followup(inter,
                    embed=e_warn("⚠️ Không Đủ LT",
                        "Một trong hai bên không còn đủ LT để cược. Trận đấu bị huỷ."),
                    ephemeral=True)

            try:
                ch_user         = self.bot.get_user(cid) or await self.bot.fetch_user(cid)
                challenger_name = ch_user.display_name
            except Exception:
                challenger_name = ts_c.get("dao_hieu", str(cid))

            await safe_followup(inter,
                embed=e_ok("⚔️ Đang Thi Đấu!",
                    f"Bạn đã chấp nhận lời thách của **{challenger_name}**!\n"
                    f"Kết quả sẽ được phát trên tất cả kênh thế giới."),
                ephemeral=True)

            asyncio.create_task(_run_cross_combat(
                self.bot, challenge, ts_c, ts_t,
                challenger_name, inter.user.display_name,
            ))

        except Exception as e:
            log.error(f"cmd_pk_nhan error user={inter.user.id}: {e}", exc_info=True)
            await safe_followup(inter,
                embed=e_loi("❌ Lỗi", f"Có lỗi xảy ra: {e}"),
                ephemeral=True)

    # ── pk-tu ─────────────────────────────────────────────────
    @worldchat_group.command(name="pk-tu", description="Từ chối lời thách đấu liên server")
    async def cmd_pk_tu(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        try:
            await expire_old_cross_challenges()

            uid       = inter.user.id
            challenge = await get_pending_cross_challenge(uid)
            if not challenge:
                return await safe_followup(inter,
                    embed=e_warn("❌ Không Có Lời Thách",
                        "Bạn không có lời thách đấu liên server nào đang chờ."),
                    ephemeral=True)

            await resolve_cross_challenge(challenge["id"], "declined")

            cid = challenge["challenger_id"]
            try:
                ch_user = self.bot.get_user(cid) or await self.bot.fetch_user(cid)
                ch_name = ch_user.display_name
            except Exception:
                ts_c    = await get_tu_si(cid)
                ch_name = ts_c.get("dao_hieu", str(cid)) if ts_c else str(cid)

            notif = (
                f"🏳️ **{inter.user.display_name}** từ chối lời thách đấu "
                f"của **{ch_name}** (mã #{challenge['id']})."
            )
            asyncio.create_task(_broadcast_cross_pvp(self.bot, notif))

            await safe_followup(inter,
                embed=e_ok("✅ Đã Từ Chối", f"Đã từ chối lời thách của **{ch_name}**."),
                ephemeral=True)

        except Exception as e:
            log.error(f"cmd_pk_tu error user={inter.user.id}: {e}", exc_info=True)
            await safe_followup(inter,
                embed=e_loi("❌ Lỗi", f"Có lỗi xảy ra: {e}"),
                ephemeral=True)

    # ── pk-huy ────────────────────────────────────────────────
    @worldchat_group.command(name="pk-huy", description="Huỷ lời thách đấu liên server bạn đã gửi")
    async def cmd_pk_huy(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        try:
            await expire_old_cross_challenges()

            existing = await get_pending_by_challenger(inter.user.id)
            if not existing:
                return await safe_followup(inter,
                    embed=e_warn("❌ Không Có", "Bạn không có lời thách nào đang chờ."),
                    ephemeral=True)

            await resolve_cross_challenge(existing["id"], "expired")
            await safe_followup(inter,
                embed=e_ok("✅ Đã Huỷ", f"Đã huỷ lời thách đấu **#{existing['id']}**."),
                ephemeral=True)

        except Exception as e:
            log.error(f"cmd_pk_huy error user={inter.user.id}: {e}", exc_info=True)
            await safe_followup(inter,
                embed=e_loi("❌ Lỗi", f"Có lỗi xảy ra: {e}"),
                ephemeral=True)

    # ── error handler ─────────────────────────────────────────
    @cmd_setup.error
    @cmd_disable.error
    async def _perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            try:
                if not inter.response.is_done():
                    await inter.response.send_message(
                        embed=e_loi("❌ Thiếu Quyền", "Cần quyền **Manage Server**."),
                        ephemeral=True)
            except Exception:
                log.exception("Lỗi world_chat")


# ── Helper ────────────────────────────────────────────────────
def _extract_webhook_id(webhook_url: str) -> int | None:
    try:
        parts = webhook_url.rstrip("/").split("/")
        return int(parts[-2])
    except Exception:
        log.exception("Lỗi world_chat")
        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(WorldChatCog(bot))
