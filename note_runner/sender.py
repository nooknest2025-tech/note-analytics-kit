"""受け取り用ウェブアプリ（Apps Script の doPost）へ、合言葉で署名して送る。
Apps Script 側（src/na_receiver.js）の naRxSign_ と同じ署名方式。"""
import hashlib
import hmac
import html
import json
import secrets
import time
import urllib.request
import urllib.error

import re

from . import core

RECEIVER_RE = re.compile(r"^https://script\.google\.com/(?:a/macros/[A-Za-z0-9.\-]+|macros)/s/[A-Za-z0-9_\-]{20,200}/exec$")
REDIRECT_HOSTS = ("script.googleusercontent.com", "script.google.com")


class _GoogleOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Apps Script は POST のあと script.googleusercontent.com に転送して結果を返す。それ以外への転送は追わない"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if core.url_host(newurl) not in REDIRECT_HOSTS:
            raise core.NaError("RECEIVER", "受け取り先が想定外の場所へ転送しようとしたので止めました。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_receiver_url(url):
    if not RECEIVER_RE.match(core.s(url)):
        raise core.NaError("CONFIG", "受け取り先のURLが Apps Script のウェブアプリ（https://script.google.com/macros/s/…/exec）ではありません。")
    return url


def sign(secret, ts, nonce, body):
    return hmac.new(secret.encode("utf-8"), f"na1\n{ts}\n{nonce}\n{body}".encode("utf-8"), hashlib.sha256).hexdigest()


def envelope(secret, body_obj, now_ms=None):
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    nonce = secrets.token_urlsafe(18)[:24]
    return {"v": 1, "ts": ts, "nonce": nonce, "sig": sign(secret, ts, nonce, body), "body": body}


RETRY_HTTP = (404, 408, 429, 500, 502, 503, 504)
STATE_WAITS = (10, 30, 60)   # 設定の読み込み：失敗したら 10秒・30秒・60秒あけて最大3回やり直す（読むだけなので毎回新しい封筒）
DATA_WAITS = (15, 60, 120)   # 書き込み：最大3回やり直す（v1.4.2：Google 側の一時的な 404 が1分ほど続いたため長めに）。同じ封筒（同じ nonce）をそのまま送り直す。合計3分15秒で、時刻の許容（±10分）・nonce の記録（30分）の中
REPLAY_CODE = "REPLAY"       # 受け取り側（v1.4.1 まで）は使用済み nonce を REPLAY で断る＝前の送信はもう届いている


class _Transient(core.NaError):
    """やり直してよい失敗（Google 側の 404/5xx、HTML の応答、通信の切断・時間切れ、BUSY）"""


def _html_summary(raw):
    """Google のエラーページ（HTML）から、見出しと本文の頭だけを短く取り出す（ログ用）"""
    t = raw or ""
    m = re.search(r"<title[^>]*>(.*?)</title>", t, re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", t, flags=re.I | re.S)
    body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))).strip()
    if title and body.startswith(title):
        body = body[len(title):].strip()
    return (title + ("：" if title and body else "") + body)[:160]


def _where(url):
    """どこが返したか（受け取り先 /exec か、結果の受け渡し先 googleusercontent か）。URL そのもの（ID・キー）は出さない"""
    host = core.url_host(url or "")
    if host == "script.googleusercontent.com":
        return "結果の受け渡し先（script.googleusercontent.com）"
    if host == "script.google.com":
        return "受け取り先（script.google.com/…/exec）"
    return "受け取り先" + (f"（{host}）" if host else "")


def wire_len(obj):
    """obj を body に入れて送ったときに、送信データ（封筒の JSON）の中で body が占める文字数。
    body は ensure_ascii=False で文字列にし、封筒は json.dumps（日本語は \\uXXXX、" と \\ はエスケープ）で送るので、その後の長さで数える"""
    return len(json.dumps(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))) - 2


class Sender:
    def __init__(self, url, secret, opener=None, sleep=time.sleep, log=None):
        self.url = check_receiver_url(url)
        if not secret or len(secret) < 32:
            raise core.NaError("CONFIG", "受け取り用の合言葉（NA_RECEIVER_SECRET）がありません。")
        self.secret = secret
        self._opener = opener or urllib.request.build_opener(_GoogleOnlyRedirect()).open
        self._sleep = sleep
        self._log = log or (lambda m: None)
        self.retries = 0

    def _post_env(self, env):
        data = json.dumps(env).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            resp = self._opener(req, timeout=60)
            raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            where = _where(getattr(e, "url", None) or (e.geturl() if hasattr(e, "geturl") else ""))
            summ = _html_summary(raw) if "<" in raw[:200] else raw[:160]
            msg = f"受け取り先がエラーを返しました（HTTP {e.code}・{where}）" + (f": {summ}" if summ else "")
            raise (_Transient if e.code in RETRY_HTTP else core.NaError)("RECEIVER", msg)
        except core.NaError:
            raise
        except (urllib.error.URLError, OSError) as e:   # 通信の切断・時間切れ（socket.timeout も OSError）
            raise _Transient("RECEIVER", f"受け取り先との通信が途中で切れました（{type(e).__name__}）。")
        try:
            out = json.loads(raw)
        except ValueError:
            summ = _html_summary(raw)
            raise _Transient("RECEIVER", "受け取り先の応答が読めませんでした（JSON ではなく HTML などが返りました）" + (f": {summ}" if summ else "") + "。デプロイのURL・アクセス権も確認してください。")
        if not isinstance(out, dict):
            raise _Transient("RECEIVER", "受け取り先の応答の形が想定と違いました。")
        if not out.get("ok"):
            code, message = out.get("code") or "RECEIVER", out.get("message") or "受け取り先に拒否されました。"
            raise (_Transient if code == "BUSY" else core.NaError)(code, message)
        return out

    def _with_retry(self, label, waits, make_env, replay_ok):
        env = make_env()
        for i in range(len(waits) + 1):
            try:
                return self._post_env(env)
            except _Transient as e:
                if i >= len(waits):
                    raise core.NaError(e.code, e.message + f"（{i + 1}回試しました）")
                self.retries += 1
                self._log(f"⚠ {label}：{e.message} → {waits[i]}秒後にもう一度送ります（{i + 2}/{len(waits) + 1}回目）")
                self._sleep(waits[i])
                if not replay_ok:
                    env = make_env()   # 読むだけの操作は新しい封筒で
            except core.NaError as e:
                if replay_ok and i > 0 and e.code == REPLAY_CODE:
                    # 前の送信は受け取り側で処理済み（応答だけが届かなかった）。二重に書かないよう、ここで成功として扱う
                    self._log(f"　{label}：前の送信がすでに受け取られていました（二重には書きません）。")
                    return {"ok": True, "replayed": True}
                raise

    def get_state(self):
        return self._with_retry("シートの設定の読み込み", STATE_WAITS, lambda: envelope(self.secret, {"kind": "note-analytics", "action": "state"}), False)

    def send_data(self, payload):
        # 同じ封筒（同じ nonce・ts・署名）を送り直す：前の送信が届いていれば受け取り側が REPLAY で断るので、記事推移などが二重にならない。
        # 時刻のずれの許容は ±10分、nonce の記録は30分なので、やり直しの待ち時間（合計3分15秒）はその中に収まる
        n = payload.get("chunk"), payload.get("chunks")
        label = "シートへの書き込み" + (f"（{n[0]}/{n[1]}回目）" if n[0] else "")
        return self._with_retry(label, DATA_WAITS, lambda: envelope(self.secret, payload), True)
