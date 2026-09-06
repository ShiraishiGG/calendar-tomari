"""
Discord カレンダー(リマインド)Bot
- 「9/7 10:00 買い物にいく」「明日10時 散髪」のようなメッセージを送ると
  指定日時になったら送信者にメンションしてメッセージを通知する。
- Render にデプロイするための簡易HTTPサーバー(PORT)を同時に起動する。
"""

import asyncio
import io
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

# このキーワードでリプライすると、リプライ先に対応する予約をキャンセルする
# (先頭のキーワードが確認メッセージの例文表示に使われます)
CANCEL_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("CANCEL_KEYWORDS", "やっぱなし,キャンセル,取り消し,トケ").split(",")
    if kw.strip()
]
CANCEL_EMOJI = os.environ.get("CANCEL_EMOJI", "🆗")

# このメッセージを送ると予約中リマインド一覧を表示する(カンマ区切りで複数指定可能)
LIST_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("LIST_KEYWORDS", "今の予定,予定確認,予定一覧").split(",")
    if kw.strip()
]


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
#   "明日 散髪"                -> 相対日のみ(時刻省略時は9:00)
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
            dt = datetime(year, month, day, tzinfo=JST) + timedelta(hours=hour, minutes=minute)
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
                base_date.year, base_date.month, base_date.day, tzinfo=JST
            ) + timedelta(hours=hour, minutes=minute)
        except ValueError:
            return None
        return dt, text.strip()

    # 2b. 今日/明日/明後日/明々後日 + の? + HH:MM メッセージ (「明日の20:25」「今日の23:30」形式)
    m = re.match(
        r"^(今日|明日|明後日|明々後日)の?(\d{1,2}):(\d{2})\s+(\S.*)$", content
    )
    if m:
        rel, hour_s, minute_s, text = m.groups()
        hour = int(hour_s)
        minute = int(minute_s)
        base_date = (now + timedelta(days=RELATIVE_DAYS[rel])).date()
        try:
            dt = datetime(
                base_date.year, base_date.month, base_date.day, tzinfo=JST
            ) + timedelta(hours=hour, minutes=minute)
        except ValueError:
            return None
        return dt, text.strip()

    # 3. 今日/明日/明後日/明々後日 + メッセージ (時刻省略時は9:00)
    m = re.match(r"^(今日|明日|明後日|明々後日)の?\s+(\S.*)$", content)
    if m:
        rel, text = m.groups()
        base_date = (now + timedelta(days=RELATIVE_DAYS[rel])).date()
        dt = datetime(base_date.year, base_date.month, base_date.day, 9, 0, tzinfo=JST)
        return dt, text.strip()

    # 4. HH:MM メッセージ (時刻のみ。過去なら翌日)
    m = re.match(r"^(\d{1,2}):(\d{2})\s+(\S.*)$", content)
    if m:
        hour_s, minute_s, text = m.groups()
        hour, minute = int(hour_s), int(minute_s)
        try:
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            dt = base + timedelta(hours=hour, minutes=minute)
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

    # 予約メッセージ or Botの確認メッセージへの「やっぱなし」リプライでキャンセル
    if message.reference is not None and message.content.strip() in CANCEL_KEYWORDS:
        await cancel_by_reply(message)
        return

    # 「今の予定」などで予約中リマインド一覧を表示
    if message.content.strip() in LIST_KEYWORDS:
        await show_reminders_in_chat(message)
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
        "message_id": message.id,  # 元メッセージのID(リプライキャンセル判定用)
    }
    reminders.append(reminder)
    save_reminders(reminders)

    # 登録完了の合図としてリアクションのみ付与(テキスト返信はしない)
    if CONFIRM_EMOJI:
        try:
            await message.add_reaction(CONFIRM_EMOJI)
        except Exception:
            log.exception("リアクション付与に失敗しました")


async def cancel_by_reply(message: discord.Message):
    """リプライ先メッセージに対応する予約を、リプライしたユーザー自身の予約に限りキャンセルする"""
    global reminders
    ref_id = message.reference.message_id
    target = next(
        (
            r
            for r in reminders
            if r["user_id"] == message.author.id and r.get("message_id") == ref_id
        ),
        None,
    )
    if target is None:
        await message.reply(
            "なんのこと？"
      )
        return

    reminders = [r for r in reminders if r is not target]
    save_reminders(reminders)

    if CANCEL_EMOJI:
        try:
            await message.add_reaction(CANCEL_EMOJI)
        except Exception:
            log.exception("リアクション付与に失敗しました")

    remind_at = datetime.fromisoformat(target["remind_at"])
    await message.reply(
        f"はーい"
    )
  
async def show_reminders_in_chat(message: discord.Message):
    """「今の予定」などのキーワードで呼ばれる一覧表示(!remindersと同内容)"""
    mine = [r for r in reminders if r["user_id"] == message.author.id]
    if not mine:
        await message.reply("何も無いよ")
        return
    mine.sort(key=lambda r: r["remind_at"])
    lines = []
    for r in mine:
        dt = datetime.fromisoformat(r["remind_at"])
        lines.append(f"{dt.strftime('%Y/%m/%d %H:%M')} - {r['message']}")
    await message.reply("\n".join(lines))


@bot.command(name="reminders")
async def list_reminders(ctx: commands.Context):
    """自分の予約中リマインド一覧を表示"""
    mine = [r for r in reminders if r["user_id"] == ctx.author.id]
    if not mine:
        await ctx.reply("何も無いよ")
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
        await ctx.reply(f"ID:{reminder_id}は知らない話")
        return
    reminders = [r for r in reminders if r is not target]
    save_reminders(reminders)
    await ctx.reply(f"ID:{reminder_id}は忘れるね")


def _is_duplicate_reminder(candidate: dict, existing: list) -> bool:
    """再デプロイ後に同じバックアップを二重で !restore してしまった場合の重複防止"""
    return any(
        e["user_id"] == candidate["user_id"]
        and e.get("message_id") == candidate.get("message_id")
        and e["remind_at"] == candidate["remind_at"]
        and e["message"] == candidate["message"]
        for e in existing
    )


# 人間が編集しやすいバックアップ用フォーマット:
#   [ID] YYYY/MM/DD HH:MM | user:xxx channel:xxx guild:xxx msgid:xxx | メッセージ本文
BACKUP_LINE_RE = re.compile(
    r"^\[(\d+)\]\s+(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*\|\s*"
    r"user:(\d+)\s+channel:(\d+)\s+guild:(\S+)\s+msgid:(\S+)\s*\|\s*(.+)$"
)


def _format_backup_text(reminder_list: list) -> str:
    lines = [
        "# リマインドバックアップ",
        "# [ID] 日時 | user:送信者ID channel:チャンネルID guild:サーバーID msgid:元メッセージID | メッセージ本文",
        "",
    ]
    for r in sorted(reminder_list, key=lambda r: r["remind_at"]):
        dt = datetime.fromisoformat(r["remind_at"])
        lines.append(
            f"[{r['id']}] {dt.strftime('%Y/%m/%d %H:%M')} | "
            f"user:{r['user_id']} channel:{r['channel_id']} "
            f"guild:{r.get('guild_id')} msgid:{r.get('message_id')} | {r['message']}"
        )
    return "\n".join(lines)


@bot.command(name="backup")
async def backup_reminders(ctx: commands.Context):
    """現在登録されている全リマインドを、人が編集できるtxt形式で出力する。
    再デプロイ(git push)前にこれを実行し、出力されたファイルを保存しておくと、
    再デプロイ後に !restore でそのファイルを読み込んで復元できる。
    """
    if not reminders:
        await ctx.reply("バックアップするリマインドが無いよ")
        return
    data = _format_backup_text(reminders)
    buf = io.BytesIO(data.encode("utf-8"))
    filename = f"reminders_backup_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.txt"
    await ctx.reply(
        f"現在の{len(reminders)}件をバックアップしたよ。",
        file=discord.File(buf, filename=filename),
    )


@bot.command(name="restore")
async def restore_reminders(ctx: commands.Context):
    """!backup で出力した(または手で編集した)txtファイルを添付して送ると、内容をリマインドに復元(マージ)する。"""
    global reminders
    if not ctx.message.attachments:
        await ctx.reply("バックアップしたtxtファイルを添付して送ってね")
        return

    attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        text = raw.decode("utf-8")
    except Exception:
        log.exception("バックアップファイルの読み込みに失敗しました")
        await ctx.reply("読み込みに失敗した…ファイルが壊れてるかも")
        return

    added = 0
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        m = BACKUP_LINE_RE.match(line)
        if not m:
            skipped += 1
            continue

        (
            _old_id,
            year_s, month_s, day_s, hour_s, minute_s,
            user_s, channel_s, guild_s, msgid_s,
            text_body,
        ) = m.groups()

        try:
            remind_at = datetime(
                int(year_s), int(month_s), int(day_s), tzinfo=JST
            ) + timedelta(hours=int(hour_s), minutes=int(minute_s))
            candidate = {
                "user_id": int(user_s),
                "channel_id": int(channel_s),
                "guild_id": None if guild_s == "None" else int(guild_s),
                "remind_at": remind_at.isoformat(),
                "message": text_body.strip(),
                "created_at": datetime.now(JST).isoformat(),
                "message_id": None if msgid_s == "None" else int(msgid_s),
            }
        except Exception:
            skipped += 1
            continue  # 書式が壊れている行はスキップ

        if not candidate["message"]:
            skipped += 1
            continue

        if _is_duplicate_reminder(candidate, reminders):
            skipped += 1
            continue

        candidate["id"] = next_id()
        reminders.append(candidate)
        added += 1

    save_reminders(reminders)
    msg = f"{added}件のリマインドを復元したよ"
    if skipped:
        msg += f"(重複/不正な{skipped}件はスキップ)"
    await ctx.reply(msg)


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
