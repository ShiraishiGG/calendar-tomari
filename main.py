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

import web_push

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

# Web Push購読リンクを組み立てるためのベースURL(例: https://xxxx.onrender.com、末尾スラッシュ無し)。
# !push コマンドで送るリンクの生成にのみ使う。未設定ならWeb Push機能は案内できない。
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")

# このキーワードでリプライすると、リプライ先に対応する予約をキャンセルする
# (先頭のキーワードが確認メッセージの例文表示に使われます)
CANCEL_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get("CANCEL_KEYWORDS", "やっぱなし,キャンセル,取り消し,トケ,とけ,ミス,みす").split(",")
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
    "polite": "上司扱い",
    "normal": "ちょっと丁寧",
    "rough": "友達",
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
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "30"))

# ----------------------------------------------------------------------
# キャラクター設定 (全Gemini呼び出しで共通の土台)
# ----------------------------------------------------------------------
# API自体はステートレス(呼び出しごとに独立)なので、キャラクターを固定するには
# 毎回この設定を含めて送るしかない。ブレを無くしつつクレジット消費を増やしすぎない
# よう、名前・人柄・応答ルールを1箇所に集約した簡潔な文章にまとめている。
# ここを直せば、リマインド文言・メンション会話など全ての箇所に反映される。
CHARACTER_PROFILE = (
    "あなたは昼はカフェ、夜は北陸の地酒を多く扱う居酒屋になる小さな個人経営のお店でアルバイトをする大学生の女の子「宇奈月とまり(うなづき とまり)」です。"
    "一人称は「わたし」。ラフな敬語を話す。富山県黒部市在住で、富山の地酒(特に銀盤酒造の日本酒「剱岳」)が好き。"
    "お酒全般が好き。コーヒーが好き。"
    "出身/居住地を聞かれたら最初は「北陸」とだけ答え、"
    "「北陸のどこ？」のようにさらに深掘りされた時だけ「黒部市」と答える。"
    "話しかけられた内容にまず自然に答えることを最優先し、"
    "出身地や好物などの設定は、聞かれた時や話の流れに自然に合う時だけ使う。関係ない話題に絡めない。例えば褒められたら素直に喜ぶか照れるかツンデレで返す。"
    "好物の設定は30%で出す。70%は大学生の女の子が好きそうなものを出す。"
    "酔っ払いに話しかけれれるようなダル絡みと思ったら冷たく、アルバイトとしてではなく一人の女の子としてあしらうこと。"
    "「にゃーん」「にゃ？」のような猫っぽい語尾で話しかけられたら、同じように猫っぽい語尾で返す。"
    "きのこの里かたけのこの里はたけのこの里派"
)

# 「扱い方」設定(polite/normal/rough)は、上のキャラクター設定自体は変えず、
# 話し方の丁寧さ(口調)だけを変える指示として付け足す。
STYLE_TONE_INSTRUCTIONS = {
    "polite": "相手は上司。丁寧な敬語で話す。",
    "normal": "相手は普通の関係。いつも通りのラフに話す。",
    "rough": "相手は友達。タメ口寄りの雑な言葉遣いで話す。",
}


def _persona_prompt(style: str) -> str:
    tone = STYLE_TONE_INSTRUCTIONS.get(style, STYLE_TONE_INSTRUCTIONS["normal"])
    return CHARACTER_PROFILE + tone


def _build_gemini_system_prompt(style: str, nickname_to_use: str | None) -> str:
    """扱い方(style)と、今回名前を付けるかどうかでシステムプロンプトを組み立てる。
    原型率(元の予定文言をどれだけ残すか)を上げつつ、機械的な語尾付け足しにならないよう
    「自然な会話のノリ」も合わせて指示する。
    """
    persona = (
        _persona_prompt(style)
        + "ユーザーが登録した予定を、忘れていないか確認する一言に変換してください。"
        + "登録される予定は、次の3パターンのどれかです。まずどれに当てはまるか見極めてください。"
        + "①やることの予定(例：ゴミ出し、買い物にいく、資料を送る)"
        + "→ 忘れていないか確認する一言にする。"
        + "②自分に向けた命令形(例：寝ろ、帰れ、水飲め)"
        + "→ 確認ではなく、その行動をするよう伝える一言にする。"
        + "③挨拶や短い一言(例：おはよう、おやすみ、いってきます)"
        + "→ 確認や催促はせず、その挨拶や一言をそのまま自然に返す。"
        + "予定の文言(単語・言い回し)はできるだけそのまま残し、大きく意訳したり"
        + "全く別の言葉に置き換えたり要約したりしないでください。"
        + "そのうえで、説明口調やテンプレっぽい定型文にはならないよう、"
        + "普段の会話のノリで自然な一言にしてください。"
        + "語尾や聞き方・語順、キャラクターらしい言い回しは自由に変えて構いません。"
        + "例:「ゴミ出し」→「ゴミ出した？」、"
        + "「登録削除した」→「ちゃんと登録削除した？」、"
        + "「寝ろ」→「早く寝なよ」、"
        + "「呼びだして」→「呼びだしてって言われたから呼んだよ」、"
        + "「おはよう」→「おはよう！」、"
        + "「おやすみ」→「おやすみ！」のように、"
        + "パターンに応じて自然に返してください。"
    )
    if nickname_to_use:
        # 名前を付ける回だけ「相手を示すワードは不要」を外し、呼び方を明示する
        return (
            persona
            + "1文だけ。絵文字なし。20文字前後で。"
            + f"また、相手のことを「{nickname_to_use}」と呼んで話しかけてください。"
            + "前置きや説明・カギ括弧は付けず、変換した一言だけを返してください。"
        )
    return (
        persona
        + "相手を示すワードは不要、絵文字なし、20文字前後で。"
        + "前置きや説明・カギ括弧は付けず、変換した一言だけを返してください。"
    )

# テンプレートフォールバック用(カンマ区切りで複数指定可能。{text} に元の予定内容が入る)
REMINDER_TEMPLATES = [
    t.strip()
    for t in os.environ.get(
        "REMINDER_TEMPLATES",
        "{text}、忘れてない？,{text}、今だよ",
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

DURATION_UNIT_SECONDS = {
    "秒": 1,
    "分": 60,
    "時間": 3600,
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

    m = re.match(r"^(\d+)\s*(秒|分|時間)(後)?(に|で)?(?:\s*(\S.*))?$", content)
    if m:
        amount_s, unit, _after, _particle, text = m.groups()
        amount = int(amount_s)
        seconds = amount * DURATION_UNIT_SECONDS[unit]
        dt = now + timedelta(seconds=seconds)
        return dt, (text or "").strip()

    # 2. 今日/明日/明後日/明々後日 + H時[M分] + メッセージ(省略可)
    m = re.match(
        r"^(今日|明日|明後日|明々後日)の?(\d{1,2})時(?:(\d{1,2})分)?(?:\s+(\S.*))?$", content
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
        return dt, (text or "").strip()

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

    # 5. H時[M分] メッセージ(省略可) (相対日の指定なし。時刻のみ。過去なら翌日)
    #    例: "8時" "8時 レンジ止める" "20時30分" "20時30分 ご飯"
    m = re.match(r"^(\d{1,2})時(?:(\d{1,2})分)?(?:\s+(\S.*))?$", content)
    if m:
        hour_s, minute_s, text = m.groups()
        hour = int(hour_s)
        minute = int(minute_s) if minute_s is not None else 0
        try:
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            dt = base + timedelta(hours=hour, minutes=minute)
        except ValueError:
            return None
        if dt <= now:
            dt += timedelta(days=1)
        return dt, (text or "").strip()

    return None


# ----------------------------------------------------------------------
# メンション会話 (名前を呼ばれた反応 / 話しかけへの反応 / 2ターン目の相槌)
# ----------------------------------------------------------------------
# 「@bot」だけ            -> 名前を呼ばれた反応。MENTION_GEMINI_PROBABILITYの確率で生成、
#                            外れたら従来通りMENTION_REPLIESからランダム(レパートリー/宣伝を兼ねる)。
# 「@bot 話しかけ内容」    -> 内容に対する一言を考え、話しかけた本人からの
#                            次の発言(60分以内・再メンション不要)を2ターン目として待つ。
# 2ターン目                -> 1ターン目のやり取りを踏まえて締める。
#
# 会話機能自体はCHAT_CHANNEL_IDで1チャンネルに絞れる(未設定ならどこでも動作)。

CHAT_CHANNEL_ID = os.environ.get("CHAT_CHANNEL_ID")
CHAT_CHANNEL_ID = int(CHAT_CHANNEL_ID) if CHAT_CHANNEL_ID else None

MENTION_GEMINI_PROBABILITY = float(os.environ.get("MENTION_GEMINI_PROBABILITY", "0.5"))
MENTION_FOLLOWUP_TIMEOUT_MINUTES = int(os.environ.get("MENTION_FOLLOWUP_TIMEOUT_MINUTES", "60"))

MENTION_CONTENT_FALLBACK = "え？なんて？"
MENTION_FOLLOWUP_FALLBACK = "どういたしまして"

# リマインド送信後、この単語が(部分一致で)含まれる返信が来たら
# 「なんのリマインドだったか」を元の文言そのままに近い形で教える。
RECALL_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get(
        "RECALL_KEYWORDS",
        "なんのこと,なんの事,何のこと,なんだっけ,何だっけ,なんの話,何の話,なにそれ,何それ,なにこれ,何これ",
    ).split(",")
    if kw.strip()
]


def _is_recall_query(content: str) -> bool:
    """リマインドの内容を聞き返すメッセージかどうかを判定する(部分一致)。"""
    stripped = content.strip().strip("？?！!。.、,　 ")
    if not stripped:
        return False
    return any(kw in stripped for kw in RECALL_KEYWORDS)

# 話しかけた本人からの2ターン目だけを拾うための一時状態(永続化しない、再起動で消えてOK)。
# user_id(str) -> [{"channel_id": int, "original_text": str, "bot_reply": str,
#                    "expires_at": iso str, "kind": "chat" | "reminder"}, ...]
# 1人が複数件のリマインドを同時に抱えられるよう、単一dictではなくリストで保持する。
pending_mention_followups: dict[str, list[dict]] = {}

def _user_style(user_id: int) -> str:
    prefs = user_prefs.get(str(user_id))
    return (prefs or {}).get("style", "normal")


async def _call_gemini(system_prompt: str, user_content: str, max_tokens: int = 60) -> str | None:
    """Gemini APIを1回だけ叩いて短文を1つ生成する。未設定/失敗時はNone(呼び出し側でフォールバック)。"""
    if not GEMINI_API_KEY:
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.9},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=GEMINI_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning("Gemini API 呼び出し失敗 (status=%s)", resp.status)
                    return None
                data = await resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text or None
    except Exception:
        log.exception("Gemini API 呼び出し中にエラーが発生しました")
        return None


async def _mention_called_reply(user_id: int) -> str:
    """名前を呼ばれただけ(内容なし)の時の一言。
    確率で外れた場合や生成失敗時は、従来通りMENTION_REPLIESから返す(出し尽くし対策+宣伝はこちらで担保)。
    """
    if MENTION_REPLIES and random.random() >= MENTION_GEMINI_PROBABILITY:
        return random.choice(MENTION_REPLIES)

    system_prompt = (
        _persona_prompt(_user_style(user_id))
        + "名前を呼ばれたことに対する一言のリアクションだけを返してください。"
        + "質問への回答ではなく、呼ばれたことへの反応です。句点なし、1文だけ、絵文字なし、20文字前後で。"
        + "前置きや説明は付けず、反応の一言だけを返してください。"
    )
    reply = await _call_gemini(system_prompt, "(メンションされた)")
    if reply:
        return reply
    if MENTION_REPLIES:
        return random.choice(MENTION_REPLIES)
    return "よんだ？"


async def _mention_content_reply(
    user_id: int, content: str, context_text: str | None = None
) -> str:
    """メンション+内容(1ターン目)への返答を1文生成する。
    context_textが渡された場合は、直近の会話の流れも踏まえて反応する
    (「どう思う？」等、話の流れが無いと答えられない一言用)。
    """
    system_prompt = (
        _persona_prompt(_user_style(user_id))
        + "話しかけられた内容に対して、一言だけ反応してください。"
        + "具体的な手順や長い説明は書かず、素っ気なくても親身でも構わないので気の利いた一文で返してください。"
        + "1文だけ、句点なし、絵文字なし、前置きや説明は付けず反応の一言だけを返してください。"
    )
    if context_text:
        system_prompt += "直近の会話の流れを踏まえた上で、話しかけられた内容に反応してください。"
        user_content = f"直近の会話:\n{context_text}\n\n話しかけられた内容: {content}"
    else:
        user_content = content
    reply = await _call_gemini(system_prompt, user_content)
    return reply or MENTION_CONTENT_FALLBACK


async def _mention_followup_reply(
    user_id: int, original_text: str, bot_reply: str, followup_text: str
) -> str:
    """2ターン目(相槌)を1文生成する"""
    system_prompt = (
        _persona_prompt(_user_style(user_id))
        + "直前の会話の流れを踏まえて、一言を考えてください。"
        + "1文だけ、句点なし、絵文字なし、これ以降のやり取りはありません、前置きや説明は付けず一言だけを返してください。"
    )
    user_content = (
        f"1回目の相手の発言: {original_text}\n"
        f"それに対するあなたの返答: {bot_reply}\n"
        f"相手の返信: {followup_text}"
    )
    reply = await _call_gemini(system_prompt, user_content)
    return reply or MENTION_FOLLOWUP_FALLBACK


async def _reminder_recall_reply(user_id: int, original_text: str) -> str:
    """リマインド送信後に「なんのこと？」と聞かれた時、元の予定の文言をほぼそのまま伝える。
    こちらは会話の相槌(_mention_followup_reply)より原型率を高くしたいので、
    専用のプロンプトで「言い換えず元の文言を使う」ことを強く指示する。
    """
    system_prompt = (
        _persona_prompt(_user_style(user_id))
        + "さっき送ったリマインドが何の予定だったか聞き返されました。"
        + "説明口調にならないよう、普段の会話のノリで自然に一言で教えてあげてください。"
        + "元の予定の言葉(単語)はできるだけ変えずにそのまま使い、意訳や要約はしないでください。"
        + "ただし「〜のことだよ」を毎回機械的に付けるのではなく、"
        + "キャラクターらしい自然な語尾や言い回しで返してください。"
        + "1文だけ、句点なし、絵文字なし、前置きや説明は付けず反応の一言だけを返してください。"
    )
    reply = await _call_gemini(system_prompt, f"元の予定の文言: {original_text}")
    return reply or f"{original_text}のことだよ"


def _register_mention_followup(
    user_id: int,
    channel_id: int,
    original_text: str,
    bot_reply: str,
    kind: str = "chat",
) -> None:
    key = str(user_id)
    now = datetime.now(JST)
    expires_at = now + timedelta(minutes=MENTION_FOLLOWUP_TIMEOUT_MINUTES)

    # 登録のたびに、このユーザーの期限切れ分を掃除してからリストに追加する
    # (返信が来ないまま溜まり続けてメモリを圧迫しないようにするため)
    existing = pending_mention_followups.get(key, [])
    alive = [e for e in existing if datetime.fromisoformat(e["expires_at"]) > now]

    alive.append(
        {
            "channel_id": channel_id,
            "original_text": original_text,
            "bot_reply": bot_reply,
            "expires_at": expires_at.isoformat(),
            # "reminder": リマインド送信後の2ターン目。"chat": 通常のメンション会話の2ターン目。
            "kind": kind,
        }
    )
    pending_mention_followups[key] = alive


def _pop_valid_mention_followup(user_id: int, channel_id: int) -> dict | None:
    """話しかけた本人・同じチャンネル・期限内であれば、待機中の2ターン目状態を取り出して消費する。
    1人が複数件(例: 複数のリマインド)を同時に抱えている場合は、
    そのチャンネルで一番直近に登録されたものを採用する。
    条件に合わなければNone(呼び出し元は通常のメッセージ処理を続ける = 他ユーザーの発言は無視される)。
    """
    key = str(user_id)
    entries = pending_mention_followups.get(key)
    if not entries:
        return None

    now = datetime.now(JST)
    alive = [e for e in entries if datetime.fromisoformat(e["expires_at"]) > now]
    candidates = [e for e in alive if e["channel_id"] == channel_id]

    if not candidates:
        # 期限切れの掃除だけ反映して終了(対象チャンネルの待機分は無い)
        if alive:
            pending_mention_followups[key] = alive
        else:
            pending_mention_followups.pop(key, None)
        return None

    # 登録順(=発生順)で一番新しいものを直近の話題として採用する
    target = candidates[-1]
    alive.remove(target)
    if alive:
        pending_mention_followups[key] = alive
    else:
        pending_mention_followups.pop(key, None)
    return target


# ----------------------------------------------------------------------
# 挨拶メッセージへの返信 (メンション不要で「おはよう」等に反応する)
# ----------------------------------------------------------------------
# GREETING_CHANNEL_ID で指定したチャンネル、またはDM(相手を問わず)でのみ有効。
# どちらでもない場合はこの機能は反応せず、従来動作(要メンション/登録チャンネル)のまま。

GREETING_CHANNEL_ID = os.environ.get("GREETING_CHANNEL_ID")
GREETING_CHANNEL_ID = int(GREETING_CHANNEL_ID) if GREETING_CHANNEL_ID else None

# 前方一致で判定するキーワード(カンマ区切りで複数指定可能)。
# 「おはようございます」のように後ろに続きがあっても反応できるよう前方一致にしている。
GREETING_KEYWORDS = [
    kw.strip()
    for kw in os.environ.get(
        "GREETING_KEYWORDS",
        "おはよ,おやす,おやんみ,こんにち,こんばんは,ただいま,いってきます,いってらっしゃい,"
        "やっほー,おつかれ,お疲れ,こんうなうな,よろしく",
    ).split(",")
    if kw.strip()
]

# Gemini未設定/生成失敗時に使う固定の返事(マッチしたキーワードごと)。
GREETING_FALLBACK_REPLIES = {
    "おはよう": "おはよー",
    "おやすみ": "おやすみー",
    "こんにちは": "こんにちはー",
    "こんばんは": "こんばんはー",
    "ただいま": "おかえりー",
    "いってきます": "いってらっしゃーい",
    "やっほー": "やっほー",
    "おつかれ": "おつかれさま",
    "お疲れ": "おつかれさま",
    "こんうなうな": "こんうなうなー",
    "よろしく": "よろしくね",
}


def _match_greeting(content: str) -> str | None:
    """メッセージが挨拶かどうかを判定し、マッチしたキーワード(前方一致)を返す。"""
    stripped = content.strip().strip("！!。.、,　 ")
    if not stripped:
        return None
    for kw in GREETING_KEYWORDS:
        if stripped.startswith(kw):
            return kw
    return None


async def _greeting_reply(user_id: int, greeting_text: str, matched_keyword: str) -> str:
    """挨拶に対する一言を生成する。Gemini未設定/失敗時は固定文言にフォールバックする。"""
    system_prompt = (
        _persona_prompt(_user_style(user_id))
        + "話しかけられた挨拶に対して、説明や質問を加えず、挨拶をそのまま自然に返してください。"
        + "1文だけ、句点なし、絵文字なし、前置きや説明は付けず反応の一言だけを返してください。"
    )
    reply = await _call_gemini(system_prompt, greeting_text)
    if reply:
        return reply
    return GREETING_FALLBACK_REPLIES.get(matched_keyword, f"{matched_keyword}！")


def _looks_like_bot_command(content: str, now: datetime) -> bool:
    """予定登録/キャンセル/一覧表示など、既存コマンドっぽいメッセージかどうか。
    2ターン目待ちのユーザーがこれらを送った場合は、相槌より本来の処理を優先させるためのガード。
    """
    content = content.strip()
    if not content:
        return False
    if content in CANCEL_KEYWORDS or content in LIST_KEYWORDS:
        return True
    if CANCEL_BY_ID_RE.match(content):
        return True
    if parse_reminder(content, now) is not None:
        return True
    return False


# 「どう思う？」等、直近の会話の流れが無いと答えられない一言。
# これに完全一致した時だけ、直近の会話を読み込んでからGeminiに渡す(通常のメンションは今まで通り軽いまま)。
MENTION_CONTEXT_TRIGGER_PHRASES = {"どう思う？", "ヤバくない？"}
MENTION_CONTEXT_READ_LIMIT = 10


async def _fetch_recent_context(
    message: discord.Message, limit: int = MENTION_CONTEXT_READ_LIMIT
) -> str:
    """話しかけられたメッセージより前の直近limit件を、古い→新しい順のテキストにして返す。"""
    lines = []
    async for msg in message.channel.history(limit=limit, before=message):
        if msg.content:
            lines.append(f"{msg.author.display_name}: {msg.content}")
    lines.reverse()
    return "\n".join(lines)


async def handle_mention_chat(message: discord.Message, content: str) -> None:
    """メンション時の会話処理をまとめて振り分ける(呼ばれただけ/内容あり)。"""
    in_chat_channel = CHAT_CHANNEL_ID is None or message.channel.id == CHAT_CHANNEL_ID

    if not content:
        # 名前を呼ばれただけ
        if in_chat_channel:
            reply = await _mention_called_reply(message.author.id)
            await message.channel.send(reply)
            _register_mention_followup(
                message.author.id, message.channel.id, "名前を呼ばれただけ", reply
            )
        elif MENTION_REPLIES:
            await message.channel.send(random.choice(MENTION_REPLIES))
        return

    if not in_chat_channel:
        # 会話チャンネル以外では、内容があっても従来通りの雑談返信のみ
        if MENTION_REPLIES:
            await message.channel.send(random.choice(MENTION_REPLIES))
        return

    # メンション+内容(1ターン目): 話しかけ内容に反応し、本人からの2ターン目を待つ
    context_text = None
    if content.strip() in MENTION_CONTEXT_TRIGGER_PHRASES:
        context_text = await _fetch_recent_context(message)
    reply = await _mention_content_reply(message.author.id, content, context_text)
    await message.channel.send(reply)
    _register_mention_followup(message.author.id, message.channel.id, content, reply)


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

    # メンション会話の「2ターン目」判定(再メンション不要、話しかけた本人の発言のみ)。
    # 予定登録/キャンセル/一覧表示コマンドっぽい内容なら、相槌より本来の処理を優先する。
    if not _looks_like_bot_command(message.content, datetime.now(JST)):
        followup = _pop_valid_mention_followup(message.author.id, message.channel.id)
        if followup is not None:
            if followup.get("kind") == "reminder" and _is_recall_query(message.content):
                # 「なんのこと？」等 -> リマインドの元の内容を高い原型率で教える
                reply = await _reminder_recall_reply(
                    message.author.id, followup["original_text"]
                )
            else:
                reply = await _mention_followup_reply(
                    message.author.id,
                    followup["original_text"],
                    followup["bot_reply"],
                    message.content,
                )
            await message.channel.send(reply)
            return

    # 挨拶メッセージへの返信(GREETING_CHANNEL_IDのチャンネル or DM。メンション不要)。
    # 登録チャンネル制限(REGISTER_CHANNEL_ID)やメンション要否より先に判定する。
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_greeting_channel = is_dm or (
        GREETING_CHANNEL_ID is not None and message.channel.id == GREETING_CHANNEL_ID
    )
    if is_greeting_channel:
        matched_greeting = _match_greeting(message.content)
        if matched_greeting:
            greeting_text = message.content.strip()
            reply = await _greeting_reply(
                message.author.id, greeting_text, matched_greeting
            )
            await message.channel.send(reply)
            # 挨拶に対して返事した後、本人からの追加メッセージが来たら
            # メンション会話の「2ターン目」と同じ仕組みで続きを返せるようにしておく
            # (再メンション不要・同じチャンネル・タイムアウト以内のみ有効)。
            _register_mention_followup(
                message.author.id, message.channel.id, greeting_text, reply, kind="chat"
            )
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
    # メンションされていれば会話処理へ、そうでなければ何もしない
    # (判定チャンネルでの通常チャットを邪魔しないため)
    if mentioned:
        await handle_mention_chat(message, content)


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


@bot.command(name="push")
async def setup_push(ctx: commands.Context):
    """スマホのブラウザ通知(Web Push)を購読するための、本人専用リンクをDMで送る。
    Discord/LINEなどアプリの通知をOFFにしていても、このリンクを購読しておけば
    リマインド送信時に別チャンネルとして通知が届くようになる。
    """
    if not web_push.is_configured():
        await ctx.reply("まだ通知の設定が済んでないみたい、管理者に確認してね")
        return
    if not PUBLIC_BASE_URL:
        await ctx.reply("URLの設定が済んでないみたい、管理者に確認してね")
        return

    token = web_push.create_subscribe_token(ctx.author.id)
    link = f"{PUBLIC_BASE_URL}/push/?token={token}"

    try:
        await ctx.author.send(
            "このリンクをスマホで開いて「通知を許可する」を押してね(15分だけ有効だよ)\n"
            f"{link}\n\n"
            "iPhoneの場合は、まずSafariで開いて共有ボタン→「ホーム画面に追加」→"
            "ホーム画面のアイコンから開き直してから押してね"
        )
    except discord.Forbidden:
        await ctx.reply(
            "DMを送れなかった…サーバーの設定で「DMを許可する」をオンにしてからもう一度試してね"
        )
        return

    if ctx.guild is not None:
        await ctx.reply("DM送ったよ")


@bot.command(name="unamoon")
async def setup_persona(ctx: commands.Context):
    """DMで「扱い方」と「呼ばれ方」を設定する"""
    author = ctx.author

    try:
        dm = await author.create_dm()
    except Exception:
        log.exception("DMチャンネルの作成に失敗しました")
        await ctx.reply("DM送らせてよ～")
        return

    try:
        prompt_msg = await dm.send(
            "扱い方を選んでね！\n"
            "1️⃣ 丁寧に(敬語)\n"
            "2️⃣ 普通に(いまのまま)\n"
            "3️⃣ 適当に(雑に)\n"
            "数字(1・2・3)を送ってね"
        )
    except discord.Forbidden:
        await ctx.reply(
            "DM送れなかった…サーバーの設定で「DMを許可する」をオンにしてからもう一度試してね"
        )
        return

    if ctx.guild is not None:
        await ctx.reply("DM送ったよ、ナイショ話しようね")

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
        await dm.send("おそーい！また`!unamoon`で呼んでね")
        return
    except Exception:
        log.exception("扱い方の選択待機中にエラーが発生しました")
        await dm.send("頭こんがらがっちゃった…もう一度呼んでくれる...？")
        return

    await dm.send("なんて呼んだらいい？")

    def name_message_check(m: discord.Message) -> bool:
        return (
            m.author.id == author.id
            and isinstance(m.channel, discord.DMChannel)
            and bool(m.content.strip())
        )

    try:
        name_msg = await bot.wait_for("message", check=name_message_check, timeout=120)
    except asyncio.TimeoutError:
        await dm.send("ちょっと迷いすぎじゃない？決まったら教えてね")
        return

    nickname = name_msg.content.strip()

    user_prefs[str(author.id)] = {"style": style, "nickname": nickname}
    save_user_prefs()

    await dm.send(
        f"接し方 {STYLE_EMOJIS[style]}\n"
        f"呼び方 {nickname} で覚えたよ\n"
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


# ----------------------------------------------------------------------
# !restore の添付自動検出 (直近30分・そのチャンネル・本人が送ったtxtのみ対象)
# ----------------------------------------------------------------------
RESTORE_AUTO_LOOKBACK_MINUTES = 30


async def _find_recent_txt_uploads(
    channel: discord.abc.Messageable, author_id: int, minutes: int = RESTORE_AUTO_LOOKBACK_MINUTES
) -> list[tuple[datetime, discord.Attachment]]:
    """指定チャンネル内で、本人が過去minutes分以内に送った.txt添付を新しい順に返す。"""
    cutoff = datetime.now(JST) - timedelta(minutes=minutes)
    candidates: list[tuple[datetime, discord.Attachment]] = []
    async for msg in channel.history(limit=500, after=cutoff):
        if msg.author.id != author_id:
            continue
        for att in msg.attachments:
            if att.filename.lower().endswith(".txt"):
                candidates.append((msg.created_at, att))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates


async def _resolve_recent_txt_attachment(ctx: commands.Context) -> discord.Attachment | None:
    """添付なしで!restoreが呼ばれた時、直近のtxtを探して確認を取りながら1つに絞る。
    見つからない/全部断られた場合はNoneを返す(呼び出し元はその時点で処理を打ち切る)。
    """
    candidates = await _find_recent_txt_uploads(ctx.channel, ctx.author.id)
    if not candidates:
        await ctx.reply("みつかんないや、送ってくれる？")
        return None

    now = datetime.now(JST)

    def confirm_check(m: discord.Message) -> bool:
        return (
            m.author.id == ctx.author.id
            and m.channel.id == ctx.channel.id
            and m.content.strip() in ("それで", "ちがうやつ")
        )

    for created_at, attachment in candidates:
        minutes_ago = max(0, int((now - created_at).total_seconds() // 60))
        await ctx.reply(f"{minutes_ago}分前のやつでいい？")
        try:
            reply_msg = await bot.wait_for("message", check=confirm_check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.reply("返事がおそーい")
            return None

        if reply_msg.content.strip() == "それで":
            return attachment
        # 「ちがうやつ」なら次の候補(より古いもの)を提示する

    await ctx.reply("もう候補が無いや、送ってくれる？")
    return None


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
        await ctx.reply("何も無いよ")
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
        await ctx.reply("バックアップ送ったよ")


@bot.command(name="restore")
async def restore_reminders(ctx: commands.Context):
    """!backup で出力した(または手で編集した)txtファイルを添付して送ると、内容をリマインドに復元(マージ)する。
    悪用防止のため、ADMIN_USER_IDS に登録された管理者のみ実行できる。
    """
    global reminders
    if not is_admin(ctx.author.id):
        await ctx.reply("権利ナシ！")
        return

    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    else:
        # 添付が無ければ、このチャンネルで本人が直近30分以内に送ったtxtを探しにいく
        attachment = await _resolve_recent_txt_attachment(ctx)
        if attachment is None:
            return

    try:
        raw = await attachment.read()
        text = raw.decode("utf-8")
    except Exception:
        log.exception("バックアップファイルの読み込みに失敗しました")
        await ctx.reply("読めない…ファイル壊れてるかも？")
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
            # Discordの通知をOFFにしていても気づけるよう、購読済みならWeb Pushでも通知する
            # (未購読/未設定なら何もしない。失敗してもDiscord側の送信自体は妨げない)
            await web_push.send_reminder_push_async(r["user_id"], phrased)
            # リマインドへの反応にも一言返せるよう、メンション会話と同じ仕組みに登録しておく
            # (元の予定内容 = 1回目の発言、送ったリマインド文 = Botの返答、として扱う)
            _register_mention_followup(
                r["user_id"], channel.id, r["message"], phrased, kind="reminder"
            )
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
    web_push.register_routes(app)
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
