"""
Discord カレンダー(リマインド)Bot
- 「9/7 10:00 買い物にいく」「明日10時 散髪」のようなメッセージを送ると
  指定日時になったら送信者にメンションしてメッセージを通知する。
- Render にデプロイするための簡易HTTPサーバー(PORT)を同時に起動する。
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from aiohttp import web
from discord.ext import commands, tasks

# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("calendar-bot")

TOKEN = os.environ.get("DISCORD_TOKEN")
PORT = int(os.environ.get("PORT", "8080"))
DATA_FILE = os.environ.get("REMINDERS_FILE", "reminders.json")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")

# 通知(リマインド送信)を固定で行うチャンネルID。
# 未設定の場合は従来通り「登録したチャンネル」に通知する。
NOTIFY_CHANNEL_ID = os.environ.get("NOTIFY_CHANNEL_ID")
NOTIFY_CHANNEL_ID = int(NOTIFY_CHANNEL_ID) if NOTIFY_CHANNEL_ID else None

# リマインド登録を受け付けるチャンネルID。
# 未設定の場合はどのチャンネルでも登録を受け付ける(従来動作)。
REGISTER_CHANNEL_ID = os.environ.get("REGISTER_CHANNEL_ID")
REGISTER_CHANNEL_ID = int(REGISTER_CHANNEL_ID) if REGISTER_CHANNEL_ID else None

# 登録成功時に元メッセージへ付与するリアクション絵文字
CONFIRM_EMOJI = os.environ.get("CONFIRM_EMOJI", "🌙")

JST = ZoneInfo("Asia/Tokyo")

RELATIVE_DAYS = {
    "今日": 0,
    "明日": 1,
    "明後日": 2,
    "明々後日": 3,
}

# ----------------------------------------------------------------------
# 日時パース
# ----------------------------------------------------------------------
# 対応フォーマット例:
#   "9/7 10:00 買い物にいく"   -> 月/日 時:分 + メッセージ
#   "09/07 買い物にいく"        -> 月/日 のみ(時刻省略時は9:00)
#   "明日10時 散髪"            -> 相対日 + 時[分] + メッセージ
#   "明日の10時30分 散髪"       -> 「の」ありもOK
#   "10:00 買い物にいく"        -> 時刻のみ(過ぎていれば翌日扱い)


def parse_reminder(content: str, now: datetime):
    content = content.strip()
    if not content:
        return None

    # 1. MM/DD [HH:MM] メッセージ
    m = re.match(r"^(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?\s+(\S.*)$", content)
    if m:
        month_s, day_s, hour_s, minute_s, text = m.groups()
        month, day = int(month_s), int(day_s)
        hour = int(hour_s) if hour_s is not None else 9
        minute = int(minute_s) if minute_s is not None else 0
        year = now.year
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=JST)
        except ValueError:
            return None
        if dt <= now:
            try:
                dt = dt.replace(year=year + 1)
            except ValueError:
                return None
        return dt, text.strip()

    # 2. 今日/明日/明後日/明々後日 + H時[M分] メッセージ
    m = re.match(
        r"^(今日|明日|明後日|明々後日)の?(\d{1,2})時(?:(\d{1,2})分)?\s*(\S.*)$", content
    )
    if m:
        rel, hour_s, minute_s, text = m.groups()
        hour = int(hour_s)
        minute = int(minute_s) if minute_s is not None else 0
        base_date = (now + timedelta(days=RELATIVE_DAYS[rel])).date()
        try:
            dt = datetime(
                base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=JST
            )
        except ValueError:
            return None
        return dt, text.strip()

    # 3. HH:MM メッセージ (時刻のみ。過去なら翌日)
    m = re.match(r"^(\d{1,2}):(\d{2})\s+(\S.*)$", content)
    if m:
        hour_s, minute_s, text = m.groups()
        hour, minute = int(hour_s), int(minute_s)
        try:
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if dt <= now:
            dt += timedelta(days=1)
        return dt, text.strip()

    return None


# ----------------------------------------------------------------------
# 永続化 (JSON ファイル)
# ----------------------------------------------------------------------


def load_reminders():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("reminders.json の読み込みに失敗しました。空リストで開始します。")
        return []


def save_reminders(reminders):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)


reminders = load_reminders()
_next_id = (max((r["id"] for r in reminders), default=0)) + 1


def next_id():
    global _next_id
    value = _next_id
    _next_id += 1
    return value


# ----------------------------------------------------------------------
# Bot 本体
# ----------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # Developer Portal でも有効化が必要

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready():
    log.info("ログイン完了: %s", bot.user)
    if not reminder_loop.is_running():
        reminder_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # コマンド ("!reminders" など) はコマンド処理に回す
    if message.content.startswith(COMMAND_PREFIX):
        await bot.process_commands(message)
        return

    # 登録を受け付けるチャンネルを固定している場合、それ以外は無視する
    if REGISTER_CHANNEL_ID and message.channel.id != REGISTER_CHANNEL_ID:
        return

    now = datetime.now(JST)
    parsed = parse_reminder(message.content, now)
    if parsed is None:
        return  # リマインド形式でなければ何もしない(通常のチャットを邪魔しない)

    remind_at, text = parsed

    reminder = {
        "id": next_id(),
        "user_id": message.author.id,
        "channel_id": message.channel.id,
        "guild_id": message.guild.id if message.guild else None,
        "remind_at": remind_at.isoformat(),
        "message": text,
        "created_at": now.isoformat(),
    }
    reminders.append(reminder)
    save_reminders(reminders)

    # 登録完了の合図としてリアクションを付与
    if CONFIRM_EMOJI:
        try:
            await message.add_reaction(CONFIRM_EMOJI)
        except Exception:
            log.exception("リアクション付与に失敗しました")

    channel_note = ""
    if NOTIFY_CHANNEL_ID and NOTIFY_CHANNEL_ID != message.channel.id:
        channel_note = f" (通知先: <#{NOTIFY_CHANNEL_ID}>)"

    await message.reply(
        f"はーい"
    )


@bot.command(name="reminders")
async def list_reminders(ctx: commands.Context):
    """自分の予約中リマインド一覧を表示"""
    mine = [r for r in reminders if r["user_id"] == ctx.author.id]
    if not mine:
        await ctx.reply("予約中のリマインドはありません。")
        return
    mine.sort(key=lambda r: r["remind_at"])
    lines = []
    for r in mine:
        dt = datetime.fromisoformat(r["remind_at"])
        lines.append(f"[ID:{r['id']}] {dt.strftime('%Y/%m/%d %H:%M')} - {r['message']}")
    await ctx.reply("\n".join(lines))


@bot.command(name="cancel")
async def cancel_reminder(ctx: commands.Context, reminder_id: int):
    """指定IDのリマインドをキャンセル (自分のものだけ)"""
    global reminders
    target = next(
        (r for r in reminders if r["id"] == reminder_id and r["user_id"] == ctx.author.id),
        None,
    )
    if target is None:
        await ctx.reply(f"ID:{reminder_id} の予約は見つかりませんでした。")
        return
    reminders = [r for r in reminders if r is not target]
    save_reminders(reminders)
    await ctx.reply(f"ID:{reminder_id} の予約をキャンセルしました。")


@tasks.loop(seconds=20)
async def reminder_loop():
    global reminders
    now = datetime.now(JST)
    due = []
    remaining = []
    for r in reminders:
        remind_at = datetime.fromisoformat(r["remind_at"])
        if remind_at <= now:
            due.append(r)
        else:
            remaining.append(r)

    if not due:
        return

    for r in due:
        try:
            target_channel_id = NOTIFY_CHANNEL_ID or r["channel_id"]
            channel = bot.get_channel(target_channel_id) or await bot.fetch_channel(
                target_channel_id
            )
            await channel.send(f"<@{r['user_id']}> {r['message']}")
        except Exception:
            log.exception("リマインド送信に失敗しました: %s", r)

    reminders = remaining
    save_reminders(reminders)


@reminder_loop.before_loop
async def before_reminder_loop():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------
# Render 用 HTTPサーバー (PORTで待受、ヘルスチェック用)
# ----------------------------------------------------------------------


async def handle_health(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/healthz", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("HTTPサーバー起動: 0.0.0.0:%s", PORT)


# ----------------------------------------------------------------------
# エントリポイント
# ----------------------------------------------------------------------


async def main():
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")

    await start_web_server()

    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
