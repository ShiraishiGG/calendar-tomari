"""
Discordの特定チャンネル・特定投稿者のメッセージを、IFTTTのWebhooks経由で
X(旧Twitter)に自動投稿する。

- IFTTT側で「Webhooks → X(Twitter): Post a tweet」のAppletを作っておく前提。
- ここでは条件に合うメッセージだけ、IFTTTのWebhook URLへ1回HTTP POSTするだけ。
- 失敗してもメインBot(リマインド機能など)には影響しないよう、例外は握りつぶし
  ログに残すだけにする(web_push.py と同じ方針)。
"""

import logging
import os

import aiohttp
import discord

log = logging.getLogger("x-post")

# 自動投稿の対象にするチャンネルID(必須。未設定なら機能自体が無効)
TWEET_CHANNEL_ID = os.environ.get("TWEET_CHANNEL_ID")
TWEET_CHANNEL_ID = int(TWEET_CHANNEL_ID) if TWEET_CHANNEL_ID else None

# 自動投稿の対象にする投稿者のDiscordユーザーID(カンマ区切りで複数指定可)
# 未設定の場合は誰の投稿も対象にしない(安全側に倒す)
TWEET_AUTHOR_IDS = {
    int(uid.strip())
    for uid in os.environ.get("TWEET_AUTHOR_IDS", "").split(",")
    if uid.strip()
}

# IFTTT側で作るWebhooksイベント名。Appletの"If This"に設定する名前と一致させること
IFTTT_EVENT_NAME = os.environ.get("IFTTT_EVENT_NAME", "discord_tweet")

# https://ifttt.com/maker_webhooks の「Documentation」ページに表示される、自分専用のキー
IFTTT_WEBHOOK_KEY = os.environ.get("IFTTT_WEBHOOK_KEY")


def _should_tweet(message: discord.Message) -> bool:
    """このメッセージを自動投稿の対象にするかどうかを判定する。"""
    if TWEET_CHANNEL_ID is None or not IFTTT_WEBHOOK_KEY:
        # 設定が揃っていなければ常に無効(誤爆防止)
        return False
    if message.channel.id != TWEET_CHANNEL_ID:
        return False
    if message.author.id not in TWEET_AUTHOR_IDS:
        return False
    if not message.content.strip():
        # 画像だけの投稿など、本文が空のものはツイートしない
        return False
    return True


async def maybe_post_to_x(message: discord.Message) -> None:
    """条件に合うメッセージだけ、IFTTT Webhooks経由でXへの投稿をトリガーする。"""
    if not _should_tweet(message):
        return

    url = f"https://maker.ifttt.com/trigger/{IFTTT_EVENT_NAME}/with/key/{IFTTT_WEBHOOK_KEY}"
    payload = {"value1": message.content}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning(
                        "IFTTT Webhooks 呼び出し失敗 (status=%s, body=%s)",
                        resp.status,
                        await resp.text(),
                    )
                else:
                    log.info("IFTTT Webhooks 呼び出し成功: %s", IFTTT_EVENT_NAME)
    except Exception:
        log.exception("IFTTT Webhooks 呼び出し中にエラーが発生しました")
