
"""
Discord カレンダー(リマインド)Bot
- 「9/7 10:00 買い物にいく」のようなメッセージを送ると
  指定日時になったら送信者にメンションしてメッセージを通知する。
- Render にデプロイするための簡易HTTPサーバー(PORT)を同時に起動する。
"""

import asyncio
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import aiohttp
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
    for kw in os.environ.get("CANCEL_KEYWORDS", "やっぱなし,これやっぱなし,キャンセル,取り消し,トケ,とけ,ミス,みす").split(",")
    if kw.strip()
]
CANCEL_EMOJI = os.environ.get("CANCEL_EMOJI", "🆗")

# 「<ID><キャンセルキーワード>」の形式でメッセージを送るとそのIDのリマインドをキャンセルする
# 例: "11トケ" "8やっぱなし" ( 「今の予定」で表示されるIDを指定する )
CANCEL_BY_ID_RE = re.compile(
    r"^(\d+)\s*(?:" + "|".join(re.escape(kw) for kw in CANCEL_KEYWORDS) + r")$"
)


# このメッセージを送ると予約中リマインド一覧を表示する(カンマ区切りで複数指定可能)
LIST_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("LIST_KEYWORDS", "今の予定,予定確認,予定一覧").split(",")
    if kw.strip()
]

# 「!backup」「!restore」を実行できる管理者のDiscordユーザーID(カンマ区切り)。
# restoreは外部から偽のリマインドを注入できてしまうため、実行できる人を限定する。
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if uid.strip()
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

# Botがメンションされたらランダムで返信する内容(カンマ区切りで複数指定可能)。
# チャンネル制限(REGISTER_CHANNEL_ID)に関係なく、どのチャンネルでも反応する。
MENTION_REPLIES = [
    kw.strip()
    for kw in os.environ.get(
        "MENTION_REPLIES", "用も無いのに呼ぶなんてサイテー,存在する私？,存在する私？,存在する私？,存在する私？,剱岳買え！,https://ginban.co.jp/,剱岳買え！,https://ginban.co.jp/,剱岳買え！,https://ginban.co.jp/,こんうなうなー！,ありえなーい！"
    ).split(",")
    if kw.strip()
]

# ----------------------------------------------------------------------
# ユーザーごとの「扱い方」「呼ばれ方」設定 (!unamoon でDM設定)
# ----------------------------------------------------------------------

USER_PREFS_FILE = os.environ.get("USER_PREFS_FILE", "user_prefs.json")

# 名前付きリマインドを送る確率(Gemini APIに投げず.py側で判定してクレジットを節約する)
NAME_REMINDER_PROBABILITY = float(os.environ.get("NAME_REMINDER_PROBABILITY", "0.1"))

STYLE_LABELS = {
    "polite": "丁寧に(敬語)",
    "normal": "普通に(いまのまま)",
    "rough": "適当に(雑に)",
}
STYLE_EMOJIS = {
    "polite": "1️⃣",
    "normal": "2️⃣",
    "rough": "3️⃣",
}
EMOJI_STYLE_MAP = {v: k for k, v in STYLE_EMOJIS.items()}
STYLE_NUMBER_MAP = {"1": "polite", "2": "normal", "3": "rough"}


def load_user_prefs():
    if not os.path.exists(USER_PREFS_FILE):
        return {}
    try:
        with open(USER_PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): v for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        log.warning("user_prefs.json の読み込みに失敗しました。空で開始します。")
        return {}


def save_user_prefs():
    with open(USER_PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(user_prefs, f, ensure_ascii=False, indent=2)


user_prefs = load_user_prefs()


# ----------------------------------------------------------------------
# リマインド文言の言い換え (Gemini API + テンプレートフォールバック)
# ----------------------------------------------------------------------
# GEMINI_API_KEY が設定されていれば、送信のたびにGemini APIで会話っぽい一言に
# 言い換える。未設定/タイムアウト/エラー時は自動でテンプレートに切り替わるので、
# APIが使えない状態でもリマインド送信自体は必ず行われる。

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "5"))

# 「扱い方」設定(polite/normal/rough)ごとのペルソナ部分。
# normalは従来のプロンプトと同じ(未設定ユーザーもこれが使われる)。
STYLE_SYSTEM_PROMPTS = {
    "polite": (
        "あなたは丁寧な敬語を話すDiscordの通知Botです。"
        "ユーザーが登録した予定を、忘れていないか確認する一言に丁寧な敬語で言い換えてください。"
    ),
    "normal": (
        "あなたはラフな敬語を使う女の子のDiscordの通知Botです。"
        "ユーザーが登録した予定を、忘れていないか確認する一言に言い換えてください。"
    ),
    "rough": (
        "あなたはかなりぞんざいでタメ口・雑な話し方をする女の子のDiscordの通知Botです。"
        "ユーザーが登録した予定を、忘れていないか確認する一言に雑に言い換えてください。"
    ),
}


def _build_gemini_system_prompt(style: str, nickname_to_use: str | None) -> str:
    """扱い方(style)と、今回名前を付けるかどうかでシステムプロンプトを組み立てる。"""
    persona = STYLE_SYSTEM_PROMPTS.get(style, STYLE_SYSTEM_PROMPTS["normal"])
    if nickname_to_use:
        # 名前を付ける回だけ「相手を示すワードは不要」を外し、呼び方を明示する
        return (
            persona
            + "できれば予定を解釈し適切な返答で、1文だけ、絵文字なし、20文字前後で。"
            + f"また、相手のことを「{nickname_to_use}」と呼んで話しかけてください。"
            + "前置きや説明・カギ括弧は付けず、言い換えた一言だけを返してください。"
        )
    return (
        persona
        + "できれば予定を解釈し適切な返答で、1文だけ、指定がない限り相手を示すワードは不要、絵文字なし、20文字前後で。"
        + "前置きや説明・カギ括弧は付けず、言い換えた一言だけを返してください。"
    )

# テンプレートフォールバック用(カンマ区切りで複数指定可能。{text} に元の予定内容が入る)
REMINDER_TEMPLATES = [
    t.strip()
    for t in os.environ.get(
        "REMINDER_TEMPLATES",
        "{text}、そろそろだよ,はいはい、{text}の時間ね,{text}、忘れてない？,"
        "そろそろ{text}じゃないの？,{text}、今だよ",
    ).split(",")
    if t.strip()
]


def _fallback_phrase(text: str, nickname_to_use: str | None = None) -> str:
    if not REMINDER_TEMPLATES:
        phrased = text
    else:
        try:
            phrased = random.choice(REMINDER_TEMPLATES).format(text=text)
        except Exception:
            phrased = text
    if nickname_to_use:
        return f"{nickname_to_use}、{phrased}"
    return phrased


async def phrase_reminder_message(text: str, user_id: int | None = None) -> str:
    """リマインド本文を会話っぽく言い換える。
    Gemini APIが使えればそれを使い、未設定/失敗時はテンプレートにフォールバックする。

    ユーザーが!unamoonで「呼ばれ方」を設定している場合、
    (Gemini APIにわざわざ確率を判定させずクレジットを節約するため).py側の抽選で
    NAME_REMINDER_PROBABILITY の確率でのみ、その名前を付けて呼びかける。
    """
    prefs = user_prefs.get(str(user_id)) if user_id is not None else None
    style = (prefs or {}).get("style", "normal")
    nickname = (prefs or {}).get("nickname")

    include_name = bool(nickname) and random.random() < NAME_REMINDER_PROBABILITY
    nickname_to_use = nickname if include_name else None

    if not GEMINI_API_KEY:
        return _fallback_phrase(text, nickname_to_use)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    system_prompt = _build_gemini_system_prompt(style, nickname_to_use)
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": f"予定: {text}"}]}],
        "generationConfig": {"maxOutputTokens": 60, "temperature": 0.9},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=GEMINI_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning(
                        "Gemini API 呼び出し失敗 (status=%s) のためテンプレートを使用します",
                        resp.status,
                    )
                    return _fallback_phrase(text, nickname_to_use)
                data = await resp.json()

        phrased = (
            data["candidates"][0]["content"]["parts"][0]["text"].strip()
        )
        return phrased or _fallback_phrase(text, nickname_to_use)
    except Exception:
        log.exception("Gemini API 呼び出し中にエラーが発生したためテンプレートを使用します")
        return _fallback_phrase(text, nickname_to_use)


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
# "9/7 10:00 買い物にいく"   
# -> 月/日 時:分 + メッセージ
# "09/07 買い物にいく"        
# -> 月/日 のみ(時刻省略時は9:00)
# "明日10時 買い物にいく"            
# -> 相対日 + 時[分] + メッセージ
# "明日の10時30分 買い物"       
# -> 「の」ありもOK
# "10:00 買い物にいく"        
# -> 時刻のみは過ぎていれば翌日扱い


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

    # 判定チャンネル: REGISTER_CHANNEL_ID未設定ならどこでも、設定していればそのチャンネルのみ
    is_register_channel = (
        REGISTER_CHANNEL_ID is None or message.channel.id == REGISTER_CHANNEL_ID
    )
    mentioned = bot.user in message.mentions

    # 判定チャンネル以外では、Botへのメンションが無いメッセージは無視する
    if not is_register_channel and not mentioned:
        return

    # メンションされている場合は、本文からメンション部分を取り除いたものを判定対象にする
    content = message.content
    if mentioned:
        content = (
            content.replace(f"<@{bot.user.id}>", "")
            .replace(f"<@!{bot.user.id}>", "")
        )
        content = re.sub(r"\s+", " ", content).strip()

    # 予約メッセージ or Botの確認メッセージへの「やっぱなし」リプライでキャンセル
    if message.reference is not None and content in CANCEL_KEYWORDS:
        await cancel_by_reply(message)
        return

    # 「<ID><キャンセルキーワード>」でIDを指定してキャンセル (例: "11トケ" "8やっぱなし")
    m = CANCEL_BY_ID_RE.match(content)
    if m:
        await cancel_by_id_text(message, int(m.group(1)))
        return

    # 「今の予定」などで予約中リマインド一覧を表示
    if content in LIST_KEYWORDS:
        await show_reminders_in_chat(message)
        return

    now = datetime.now(JST)
    parsed = parse_reminder(content, now)
    if parsed is not None:
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
        return

    # ここまでのどれにも当てはまらなかった場合:
    # メンションされていればランダムに雑談返信、そうでなければ何もしない
    # (判定チャンネルでの通常チャットを邪魔しないため)
    if mentioned and MENTION_REPLIES:
        await message.channel.send(random.choice(MENTION_REPLIES))


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


async def cancel_by_id_text(message: discord.Message, reminder_id: int):
    """「<ID><キャンセルキーワード>」形式のメッセージで、自分自身の予約に限りキャンセルする"""
    global reminders
    target = next(
        (
            r
            for r in reminders
            if r["id"] == reminder_id and r["user_id"] == message.author.id
        ),
        None,
    )
    if target is None:
        await message.reply(f"ID:{reminder_id}は知らない話")
        return

    reminders = [r for r in reminders if r is not target]
    save_reminders(reminders)

    if CANCEL_EMOJI:
        try:
            await message.add_reaction(CANCEL_EMOJI)
        except Exception:
            log.exception("リアクション付与に失敗しました")

    await message.reply(f"{reminder_id}は忘れるね")

  
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
        lines.append(f"[{r['id']}] {dt.strftime('%Y/%m/%d %H:%M')} - {r['message']}")
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
        await ctx.reply(f"{reminder_id}は知らない話")
        return
    reminders = [r for r in reminders if r is not target]
    save_reminders(reminders)
    await ctx.reply(f"{reminder_id}は忘れるね")


@bot.command(name="unamoon")
async def setup_persona(ctx: commands.Context):
    """DMで「扱い方」と「呼ばれ方」を設定する"""
    author = ctx.author

    try:
        dm = await author.create_dm()
    except Exception:
        log.exception("DMチャンネルの作成に失敗しました")
        await ctx.reply("DMを開けなかった…もう一度試してみて")
        return

    try:
        prompt_msg = await dm.send(
            "扱い方を選んでね！\n"
            "1️⃣ 丁寧に(敬語)\n"
            "2️⃣ 普通に(いまのまま)\n"
            "3️⃣ 適当に(雑に)\n"
            "リアクションか、数字(1・2・3)を送ってね"
        )
    except discord.Forbidden:
        await ctx.reply(
            "DMを送れなかった…サーバーの設定で「DMを許可する」をオンにしてからもう一度試してね"
        )
        return

    if ctx.guild is not None:
        await ctx.reply("DMを送ったよ、ナイショ話しようね")

    for emoji in STYLE_EMOJIS.values():
        try:
            await prompt_msg.add_reaction(emoji)
        except Exception:
            log.exception("リアクション付与に失敗しました")

    def reaction_check(reaction: discord.Reaction, user: discord.User) -> bool:
        return (
            user.id == author.id
            and reaction.message.id == prompt_msg.id
            and str(reaction.emoji) in EMOJI_STYLE_MAP
        )

    def style_message_check(m: discord.Message) -> bool:
        return (
            m.author.id == author.id
            and isinstance(m.channel, discord.DMChannel)
            and m.content.strip() in STYLE_NUMBER_MAP
        )

    reaction_task = asyncio.ensure_future(
        bot.wait_for("reaction_add", check=reaction_check, timeout=120)
    )
    message_task = asyncio.ensure_future(
        bot.wait_for("message", check=style_message_check, timeout=120)
    )

    style = None
    try:
        done, pending = await asyncio.wait(
            {reaction_task, message_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        result = done.pop().result()
        if isinstance(result, tuple):
            # reaction_add イベント -> (reaction, user)
            reaction, _user = result
            style = EMOJI_STYLE_MAP[str(reaction.emoji)]
        else:
            # message イベント
            style = STYLE_NUMBER_MAP[result.content.strip()]
    except asyncio.TimeoutError:
        await dm.send("おそーい！、また`!unamoon`で呼んでね")
        return
    except Exception:
        log.exception("扱い方の選択待機中にエラーが発生しました")
        await dm.send("頭こんがらがっちゃった…もう一度`!unamoon`で呼んでくれる...？")
        return

    await dm.send("なんて呼ばれたい？")

    def name_message_check(m: discord.Message) -> bool:
        return (
            m.author.id == author.id
            and isinstance(m.channel, discord.DMChannel)
            and bool(m.content.strip())
        )

    try:
        name_msg = await bot.wait_for("message", check=name_message_check, timeout=120)
    except asyncio.TimeoutError:
        await dm.send("ちょっと迷いすぎじゃない？、また`!unamoon`で呼んでね")
        return

    nickname = name_msg.content.strip()

    user_prefs[str(author.id)] = {"style": style, "nickname": nickname}
    save_user_prefs()

    await dm.send(
        f"接し方 {STYLE_EMOJIS[style]}\n"
        f"じゃあ次から{nickname}って呼ぶね！\n"
    )


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

# ユーザー設定(扱い方/呼ばれ方)行のフォーマット: USERPREF:user_id|style|呼び方
USERPREF_LINE_RE = re.compile(r"^USERPREF:(\d+)\|(polite|normal|rough)\|(.*)$")


def _format_backup_text(reminder_list: list, prefs: dict) -> str:
    lines = [
        "# リマインドバックアップ",
        f"# 出力日時: {datetime.now(JST).strftime('%Y/%m/%d %H:%M')}",
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

    lines.append("")
    lines.append("# ユーザー設定 (!unamoonで設定した扱い方・呼ばれ方)")
    lines.append("# USERPREF:user_id|style(polite/normal/rough)|呼び方")
    for user_id, pref in sorted(prefs.items()):
        style = pref.get("style", "normal")
        nickname = pref.get("nickname", "")
        if not nickname:
            continue
        lines.append(f"USERPREF:{user_id}|{style}|{nickname}")

    return "\n".join(lines)


@bot.command(name="backup")
async def backup_reminders(ctx: commands.Context):
    """現在登録されている全リマインドを、人が編集できるtxt形式で管理者のDMに送る。
    再デプロイ(git push)前にこれを実行しておくと、再デプロイ後に !restore で復元できる。
    悪用防止のため、ADMIN_USER_IDS に登録された管理者のみ実行できる。
    """
    if not is_admin(ctx.author.id):
        await ctx.reply("権利ナシ！")
        return
    if not reminders and not user_prefs:
        await ctx.reply("バックアップするものが無いよ")
        return

    data = _format_backup_text(reminders, user_prefs)
    buf = io.BytesIO(data.encode("utf-8"))
    filename = f"reminders_backup_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.txt"

    try:
        prefs_count = sum(1 for p in user_prefs.values() if p.get("nickname"))
        await ctx.author.send(
            f"予定{len(reminders)}件・設定{prefs_count}件をバックアップしたよ。"
            "忘れずに`!restore`してね",
            file=discord.File(buf, filename=filename),
        )
    except discord.Forbidden:
        await ctx.reply(
            "DMを送れなかった…サーバーの設定で「DMを許可する」をオンにしてからもう一度試してね"
        )
        return

    # チャンネルには中身を残さない(DMに送った旨だけ伝える)
    if ctx.guild is not None:
        await ctx.reply("バックアップをDMに送ったよ")


@bot.command(name="restore")
async def restore_reminders(ctx: commands.Context):
    """!backup で出力した(または手で編集した)txtファイルを添付して送ると、内容をリマインドに復元(マージ)する。
    悪用防止のため、ADMIN_USER_IDS に登録された管理者のみ実行できる。
    """
    global reminders
    if not is_admin(ctx.author.id):
        await ctx.reply("権利ナシ！")
        return
    if not ctx.message.attachments:
        await ctx.reply("バックアップはどこ？")
        return

    attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        text = raw.decode("utf-8")
    except Exception:
        log.exception("バックアップファイルの読み込みに失敗しました")
        await ctx.reply("読み込みに失敗した…ファイルが壊れてるかも？")
        return

    added = 0
    skipped = 0
    prefs_added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        pref_m = USERPREF_LINE_RE.match(line)
        if pref_m:
            uid_s, style, nickname = pref_m.groups()
            nickname = nickname.strip()
            if not nickname:
                skipped += 1
                continue
            user_prefs[uid_s] = {"style": style, "nickname": nickname}
            prefs_added += 1
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
    save_user_prefs()
    msg = f"予定{added}件・設定{prefs_added}件を復元したよ"
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
            phrased = await phrase_reminder_message(r["message"], r["user_id"])
            await channel.send(f"<@{r['user_id']}> {phrased}")
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
