"""
Web Push通知 (VAPIDベース)。Discord Bot本体とは独立したモジュール。

Discord/LINEなどアプリ単位で通知をOFFにしていても、スマホのブラウザで
このモジュールが配るページを1回購読しておけば、別チャンネルとして
プッシュ通知を届けられる(iOSはホーム画面に追加したPWAとして開く必要あり)。

ユーザー側の流れ:
  1. Discordで `!push` を実行 -> DMに購読用リンクが届く(15分だけ有効)
  2. スマホでそのリンクを開く
     - iPhone: 一度Safariで開いて「共有」→「ホーム画面に追加」→
               ホーム画面のアイコンから開き直してから「通知を許可する」をタップ
     - Android: そのままブラウザで開いて「通知を許可する」をタップ
  3. 以後、そのユーザー宛のリマインドはこのモジュール経由でもプッシュ通知が届く

main.py側は以下だけ呼べばよい:
  - register_routes(app)                          : aiohttpにルートを追加
  - create_subscribe_token(user_id)                : !push コマンドで使う
  - is_configured()                                : VAPID鍵が設定済みか
  - await send_reminder_push_async(user_id, body)  : リマインド送信時に呼ぶ
"""

import asyncio
import json
import logging
import os
import secrets
import time

from aiohttp import web
from pywebpush import webpush, WebPushException

log = logging.getLogger("web-push")

# https://vapidkeys.com/ や `npx web-push generate-vapid-keys` で発行した
# base64文字列のペアをそのまま環境変数に入れる想定。
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_CLAIMS_SUB = os.environ.get("VAPID_CLAIMS_SUB", "mailto:example@example.com")

SUBSCRIPTIONS_FILE = os.environ.get("PUSH_SUBSCRIPTIONS_FILE", "push_subscriptions.json")

# ホーム画面アイコン/通知アイコンに使う正方形PNG画像のパス。
# リポジトリ直下(main.pyと同じ階層)にこの名前のファイルを置くだけで反映される。
# 512x512程度の正方形PNGを推奨(小さい分にはブラウザ側で縮小してくれる)。
ICON_PATH = os.environ.get("PUSH_ICON_PATH", "push_icon.png")

# 購読ページ発行トークンの有効時間(分)。切れたら!pushでリンクを取り直す。
TOKEN_TTL_MINUTES = 15

# token -> {"user_id": int, "expires_at": float}。永続化しない(再起動で消えてOK)。
_pending_tokens: dict[str, dict] = {}


def is_configured() -> bool:
    """VAPID鍵が両方設定されているか(未設定なら機能全体を無効化する)。"""
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


# ----------------------------------------------------------------------
# 購読情報の永続化 (discordユーザーID -> [subscription, ...])
# 1人が複数端末で購読できるようリストで持つ。
# ----------------------------------------------------------------------


def load_subscriptions() -> dict:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return {}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("%s の読み込みに失敗しました。空で開始します。", SUBSCRIPTIONS_FILE)
        return {}


def save_subscriptions(subs: dict) -> None:
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


_subscriptions: dict[str, list[dict]] = load_subscriptions()


# ----------------------------------------------------------------------
# 購読用トークン (!push コマンドで発行し、リンクに埋め込む)
# ----------------------------------------------------------------------


def create_subscribe_token(user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    _pending_tokens[token] = {
        "user_id": user_id,
        "expires_at": time.time() + TOKEN_TTL_MINUTES * 60,
    }
    return token


def _resolve_token(token: str) -> int | None:
    entry = _pending_tokens.get(token)
    if not entry:
        return None
    if entry["expires_at"] < time.time():
        _pending_tokens.pop(token, None)
        return None
    return entry["user_id"]


# ----------------------------------------------------------------------
# 購読ページ (PWAとしてホーム画面に追加できるよう manifest / service worker も配る)
# ----------------------------------------------------------------------

SUBSCRIBE_PAGE_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>とまりの通知</title>
<link rel="manifest" id="manifest-link" href="/push/manifest.webmanifest">
<link rel="apple-touch-icon" href="/push/icon.png">
<link rel="icon" href="/push/icon.png">
<style>
  body { font-family: sans-serif; text-align: center; padding: 40px 16px; }
  button { font-size: 18px; padding: 12px 24px; border-radius: 8px; border: none;
           background: #5865F2; color: #fff; }
  p.status { margin-top: 20px; color: #555; white-space: pre-wrap; }
</style>
</head>
<body>
  <h2>宇奈月とまりからの通知</h2>
  <p>下のボタンで通知を許可してね。<br>
     (iPhoneの場合は、まず共有ボタンから「ホーム画面に追加」して、
     ホーム画面のアイコンから開き直してから押してね)</p>
  <button id="subscribe-btn">通知を許可する</button>
  <p class="status" id="status"></p>
  <p class="status" id="debug"></p>
<script>
// iOSは「ホーム画面に追加」した瞬間、manifestのstart_urlをそのまま
// アイコンの起動先として焼き込む。localStorageはSafariと共有されない場合があるため、
// manifest自体をtoken付きで動的に生成させ、start_urlにtokenを埋め込ませることで対応する。
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) {
  localStorage.setItem("push_token", urlToken);
  const manifestLink = document.getElementById("manifest-link");
  manifestLink.href = "/push/manifest.webmanifest?token=" + encodeURIComponent(urlToken);
}
const TOKEN = urlToken || localStorage.getItem("push_token") || "";

// tokenを検出できているか、ボタンを押す前にその場で分かるようにしておく
document.getElementById("debug").textContent = TOKEN
  ? "リンク情報: 検出できてるよ"
  : "リンク情報: 検出できてない(アドレスバーに ?token=... が付いてるか確認してね)";

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
}

async function subscribe() {
  const statusEl = document.getElementById("status");
  if (!TOKEN) {
    statusEl.textContent = "リンクの情報が見つからないよ、Discordでもう一度「!push」を実行してリンクを開き直してね";
    return;
  }
  try {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      statusEl.textContent = "このブラウザ/開き方だと通知に対応していないみたい。"
        + "iPhoneならホーム画面に追加したアイコンから開いてみてね";
      return;
    }
    const reg = await navigator.serviceWorker.register("/push/sw.js");
    const keyRes = await fetch("/push/vapid-public-key");
    if (!keyRes.ok) throw new Error("鍵の取得に失敗");
    const { key } = await keyRes.json();
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    const res = await fetch("/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: TOKEN, subscription: sub }),
    });
    if (!res.ok) throw new Error("登録に失敗(リンクの有効期限切れかも)");
    statusEl.textContent = "設定できたよ、これで通知が届くはず！";
  } catch (e) {
    statusEl.textContent = "うまくいかなかった…: " + e;
  }
}

document.getElementById("subscribe-btn").addEventListener("click", subscribe);
</script>
</body>
</html>
"""

SERVICE_WORKER_JS = """
self.addEventListener('push', function (event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}
  const title = data.title || '宇奈月とまりから';
  const body = data.body || '';
  event.waitUntil(self.registration.showNotification(title, {
    body: body,
    icon: '/push/icon.png',
    badge: '/push/icon.png',
  }));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});
"""

def _build_manifest(token: str) -> dict:
    start_url = f"/push/?token={token}" if token else "/push/"
    return {
        "name": "宇奈月とまり",
        "short_name": "とまり",
        "start_url": start_url,
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#5865F2",
        "icons": [
            {"src": "/push/icon.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/push/icon.png", "sizes": "512x512", "type": "image/png"},
        ],
    }


async def handle_subscribe_page(request: web.Request) -> web.Response:
    # tokenはクライアント側のJS(URLパラメータ or localStorage)で解決するので、
    # サーバー側では常に同じ静的HTMLを返すだけでよい。
    return web.Response(text=SUBSCRIBE_PAGE_HTML, content_type="text/html")


async def handle_manifest(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    manifest = _build_manifest(token)
    return web.json_response(manifest, content_type="application/manifest+json")


async def handle_service_worker(request: web.Request) -> web.Response:
    return web.Response(text=SERVICE_WORKER_JS, content_type="application/javascript")


async def handle_vapid_public_key(request: web.Request) -> web.Response:
    if not is_configured():
        return web.json_response({"error": "push not configured"}, status=503)
    return web.json_response({"key": VAPID_PUBLIC_KEY})


async def handle_subscribe(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    token = payload.get("token") or ""
    subscription = payload.get("subscription")
    user_id = _resolve_token(token)
    if user_id is None:
        return web.json_response({"error": "invalid or expired token"}, status=400)
    if not subscription or "endpoint" not in subscription:
        return web.json_response({"error": "invalid subscription"}, status=400)

    key = str(user_id)
    subs = _subscriptions.setdefault(key, [])
    if not any(s.get("endpoint") == subscription["endpoint"] for s in subs):
        subs.append(subscription)
        save_subscriptions(_subscriptions)

    _pending_tokens.pop(token, None)  # 使い切りにする
    return web.json_response({"ok": True})


async def handle_icon(request: web.Request) -> web.Response:
    if not os.path.exists(ICON_PATH):
        return web.Response(status=404, text="icon not set")
    return web.FileResponse(ICON_PATH)


def register_routes(app: web.Application) -> None:
    """main.py の start_web_server() から1回呼ぶだけでよい。"""
    app.router.add_get("/push/", handle_subscribe_page)
    app.router.add_get("/push/manifest.webmanifest", handle_manifest)
    app.router.add_get("/push/sw.js", handle_service_worker)
    app.router.add_get("/push/vapid-public-key", handle_vapid_public_key)
    app.router.add_get("/push/icon.png", handle_icon)
    app.router.add_post("/push/subscribe", handle_subscribe)


# ----------------------------------------------------------------------
# 送信 (pywebpushはブロッキングI/Oなので、asyncio側はexecutorで包む)
# ----------------------------------------------------------------------


def send_reminder_push(user_id: int, body: str, title: str = "宇奈月とまりから") -> None:
    """該当ユーザーの全購読先にプッシュ通知を送る(ベストエフォート)。
    無効になった購読(410/404)は自動で削除し、それ以外のエラーは握りつぶしてログだけ残す
    (Discord側の通知が失敗しないことを最優先するため)。
    """
    if not is_configured():
        return
    key = str(user_id)
    subs = _subscriptions.get(key)
    if not subs:
        return

    payload = json.dumps({"title": title, "body": body})
    alive = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
            )
            alive.append(sub)
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                log.info("購読が無効になっていたため削除します: user=%s", user_id)
                continue
            log.warning("Web Push送信に失敗しました(一時的エラーとして保持): %s", e)
            alive.append(sub)
        except Exception:
            log.exception("Web Push送信中に予期しないエラーが発生しました")
            alive.append(sub)

    if len(alive) != len(subs):
        _subscriptions[key] = alive
        save_subscriptions(_subscriptions)


async def send_reminder_push_async(user_id: int, body: str, title: str = "宇奈月とまりから") -> None:
    """reminder_loop など非同期側から呼ぶための薄いラッパー。"""
    if not is_configured():
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, send_reminder_push, user_id, body, title)
