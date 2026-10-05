"""note への HTTP。Cookie を付けるのはここ1か所だけ・送り先が note.com のときだけ。
一時トークンは graphql.note.com だけ。間隔は3秒以上あける。403/429 は止める合図。"""
import time
import urllib.request
import urllib.error

from . import core

UA = "note-analytics-runner/1.1 (+personal use; respects robots spacing)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """転送（リダイレクト）は追いかけない。ログイン画面への転送＝期限切れとして扱うため"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """転送（リダイレクト）は追いかけない（ログイン画面への転送を「期限切れ」として検出するため）"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener():
    return urllib.request.build_opener(_NoRedirect()).open


class Stop(Exception):
    """403/429/上限：その日の取得を止める"""
    def __init__(self, code, message):
        super().__init__(message)
        self.http = code
        self.message = message


class Client:
    def __init__(self, cookie_value=None, interval_sec=3.0, max_requests=300, sleep=time.sleep, now=None, opener=None):
        self._cookie_value = cookie_value          # 生の値は内部だけ。ログ・例外には出さない
        self.interval = max(3.0, float(interval_sec or 3.0))   # ログイン中の取得と同じく最低3秒
        self.max_requests = max_requests
        self.requests = 0
        self._last = 0.0
        self._sleep = sleep
        self._now = now or (lambda: time.time() * 1000)
        self._opener = opener or _OPENER.open
        self.token = None                           # 新ダッシュボードの一時トークン（メモリのみ）

    def _wait(self):
        if self.requests >= self.max_requests:
            raise Stop(None, f"1回の取得でアクセスできる回数の上限（{self.max_requests}回）に達したので止めました。")
        gap = self.interval - (time.time() - self._last)
        if self._last and gap > 0:
            self._sleep(gap)
        self._last = time.time()
        self.requests += 1

    def _open(self, url, headers, data=None, method="GET"):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = self._opener(req, timeout=30)
            return resp.getcode(), resp.read().decode("utf-8", "replace"), {k.lower(): v for k, v in resp.headers.items()}, resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            return e.code, body, {k.lower(): v for k, v in (e.headers or {}).items()}, (e.headers.get_all("Set-Cookie") if e.headers else []) or []

    # ---------- 公開データ（Cookie を付けない） ----------
    def get_public(self, url):
        host = core.url_host(url)
        assert host == core.HOST_COOKIE, f"想定外のURL: {url}"
        for attempt in range(2):
            self._wait()
            code, body, _, _ = self._open(url, {"Accept": "application/json", "User-Agent": UA})
            if code == 200:
                return body
            if code == 404:
                raise core.NaError("NOTFOUND", "見つかりませんでした（IDが間違っているか、記事が削除された可能性があります）")
            if code in (403, 429):
                raise Stop(code, f"noteから「アクセスが多い／許可されていない」（{code}）と返されたので、今回の取得を止めました（{self.requests}回目）。")
            if code >= 500 and attempt == 0:
                self._sleep(10)
                continue
            raise core.NaError("HTTP", f"noteへのアクセスでエラーになりました（{code}）")
        raise core.NaError("HTTP", "noteへのアクセスに失敗しました。")

    # ---------- 認証（Cookie / Bearer を付ける唯一の場所） ----------
    def _auth(self, url, kind, method="GET", data=None, extra=None, ok_codes=(200,)):
        host = core.url_host(url)
        headers = {"Accept": "application/json", "User-Agent": UA}
        if kind == "cookie":
            if host != core.HOST_COOKIE:
                raise core.NaError("GUARD", f"安全のため止めました：Cookie は note.com 以外には送りません（送り先: {host or '不正なURL'}）。")
            if not self._cookie_value:
                raise core.NaError("NOCOOKIE", "Cookie がありません。")
            headers["Cookie"] = f"{core.COOKIE_NAME}={self._cookie_value}"
        elif kind == "bearer":
            if host != core.HOST_GQL:
                raise core.NaError("GUARD", f"安全のため止めました：一時トークンは graphql.note.com 以外には送りません（送り先: {host or '不正なURL'}）。")
            if not self.token:
                raise core.NaError("GUARD", "一時トークンがありません。")
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            raise core.NaError("GUARD", "認証の種類が不明です。")
        if extra:
            headers.update(extra)
        if data is not None:
            headers["Content-Type"] = "application/json"
        self._wait()
        code, body, hdrs, setck = self._open(url, headers, data=data.encode() if isinstance(data, str) else data, method=method)
        state, reason = core.auth_response_state(code, hdrs.get("content-type", ""), body, hdrs.get("location", ""))
        if state == "ok" and code not in ok_codes and not (200 <= code < 300):
            state, reason = "error", f"想定外の応答（{code}）"
        return {"code": code, "body": body, "headers": hdrs, "set_cookie": setck, "state": state, "reason": reason}

    def stats_pv(self, page):
        return self._auth(f"{core.STATS_PV_URL}?filter=all&page={page}&sort=pv", "cookie")

    def gql_auth(self):
        r = self._auth(core.GQL_AUTH_URL, "cookie", method="POST", extra={"X-Requested-With": "XMLHttpRequest"}, ok_codes=(200, 201))
        if r["state"] == "ok":
            self.token = core.gql_token_from_set_cookie(r["set_cookie"])
        return r

    def gql(self, body):
        return self._auth(core.GQL_URL, "bearer", method="POST", data=body)
