"""note の応答を読む・URL・安全チェック（ネットワークには触れない純粋関数）。
Apps Script 版（src/na_core.js・src/na_dash.js）と同じ規則・同じ項目です。"""
import json
import re
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS
API = "https://note.com/api"
COOKIE_NAME = "_note_session_v5"
HOST_COOKIE = "note.com"           # Cookie を付けてよい唯一のホスト
HOST_GQL = "graphql.note.com"      # 一時トークン（Bearer）を付けてよい唯一のホスト
STATS_PV_URL = "https://note.com/api/v1/stats/pv"
GQL_AUTH_URL = "https://note.com/api/v3/graphql/auth"
GQL_URL = "https://graphql.note.com/graphql"
METHOD_STATS = "自動（Cookie・stats/pv）"
METHOD_GQL = "自動（Cookie・新ダッシュボード）"
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,50}$")
KEY_RE = re.compile(r"^n[0-9a-z]{6,24}$")


class NaError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def s(v):
    return "" if v is None else str(v).strip()


def num(v):
    try:
        x = float(re.sub(r"[,，\s円¥￥]", "", s(v)) or "nan")
    except ValueError:
        return 0
    if x != x or x in (float("inf"), float("-inf")):
        return 0
    return int(x) if x.is_integer() else x


def num0(v):
    return num(v)


def normalize_id(v):
    t = s(v)
    m = re.search(r"note\.com/([A-Za-z0-9_\-]+)", t)
    if m:
        t = m.group(1)
    t = t.lstrip("@")
    return t if ID_RE.match(t) else ""


def jst(ms):
    d = datetime.fromtimestamp(ms / 1000, JST)
    return {"date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M"), "weekday": (d.weekday() + 1) % 7, "hour": d.hour, "stamp": d.strftime("%Y-%m-%d %H:%M")}


def day_start_ms(ms):
    return (ms + 9 * HOUR_MS) // DAY_MS * DAY_MS - 9 * HOUR_MS


def parse_time(v):
    """ISO 形式（+09:00 など）。タイムゾーンが無いものは日本時間として扱う。"""
    t = s(v)
    if not t:
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=JST)
    return int(d.timestamp() * 1000)


# ---------- URL ----------
def quote(x):
    from urllib.parse import quote as q
    return q(str(x), safe="")


def list_url(cid, page):
    return f"{API}/v2/creators/{quote(cid)}/contents?kind=note&page={page}"


def creator_url(cid):
    return f"{API}/v2/creators/{quote(cid)}"


def detail_url(key):
    return f"{API}/v3/notes/{quote(key)}"


def likes_url(key, page):
    return f"{API}/v3/notes/{quote(key)}/likes?page={page}"


def comments_url(key, page):
    """記事のコメント一覧（公開・Cookie なし）。返ってくるのは「親コメント」だけで、新しい順（2026-10 時点で確認）"""
    return f"{API}/v3/notes/{quote(key)}/note_comments?page={page}"


def url_host(url):
    """https のみ。ユーザー名@・ポート指定・バックスラッシュは空文字（＝拒否）"""
    m = re.match(r"^https://([^/?#]*)(?:[/?#]|$)", s(url), re.I)
    if not m:
        return ""
    auth = m.group(1)
    if not auth or "@" in auth or ":" in auth or "\\" in auth:
        return ""
    return auth.lower().rstrip(".")


def key_from_url(u):
    m = re.search(r"/n/(n[0-9a-z]+)", s(u), re.I)
    return m.group(1) if m else ""


# ---------- 秘密の扱い ----------
def normalize_cookie(inp):
    t = re.sub(r"^cookie:\s*", "", s(inp), flags=re.I).replace("\r", "").replace("\n", "")
    if not t:
        return None, "Cookie が空です。"
    v = ""
    if "=" in t:
        for part in t.split(";"):
            if "=" in part:
                k, _, val = part.partition("=")
                if k.strip() == COOKIE_NAME:
                    v = val.strip()
        if not v:
            return None, f"{COOKIE_NAME} が見つかりませんでした。値だけを貼り付けてください。"
    else:
        v = t
    v = v.strip('"')
    if not re.match(r"^[A-Za-z0-9%_\-\.~+/=]{16,4096}$", v):
        return None, "Cookie の値の形が想定と違います（空白や日本語が入っていないか確認してください）。"
    return v, ""


def mask(v):
    t = s(v)
    if not t:
        return ""
    return "****" + (t[-4:] if len(t) > 8 else "")


# ---------- 公開データ ----------
def parse_creator(j):
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict) or not d.get("urlname"):
        raise NaError("PARSE", "クリエイター情報を読み取れませんでした（noteの仕様が変わった可能性があります）。")
    return {"creator": d["urlname"], "name": s(d.get("nickname")), "followers": num0(d.get("followerCount")), "following": num0(d.get("followingCount")), "noteCount": num0(d.get("noteCount"))}


def parse_list(j, creator_id):
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("contents"), list):
        raise NaError("PARSE", "記事一覧を読み取れませんでした（noteの仕様が変わった可能性があります）。")
    out = []
    for c in d["contents"]:
        ms = parse_time(c.get("publishAt"))
        key = s(c.get("key"))
        if not key or ms is None:
            continue
        tags = [s((h or {}).get("hashtag", {}).get("name")) for h in (c.get("hashtags") or [])]
        tags = [t for t in tags if t]
        pi = c.get("priceInfo") or {}
        paid = bool((num0(c.get("price")) > 0) or pi.get("isFree") is False)
        price = num0(c.get("price")) or num0(pi.get("oneshotLowestPrice") or pi.get("lowestPrice"))
        user = c.get("user") or {}
        title = s(c.get("name"))
        out.append({"creator": s(user.get("urlname")) or creator_id, "key": key, "title": title,
                    "url": s(c.get("noteUrl")) or f"https://note.com/{creator_id}/n/{key}", "publishMs": ms, "type": s(c.get("type")),
                    "paid": paid, "price": price if paid else 0, "likes": num0(c.get("likeCount")), "comments": num0(c.get("commentCount")),
                    "hashtags": tags, "imageCount": num0(c.get("imageCount")), "pinned": bool(c.get("isPinned"))})
    return {"articles": out, "isLast": bool(d.get("isLastPage")) or len(d["contents"]) == 0, "total": num0(d.get("totalCount"))}


def html_to_text(h):
    t = re.sub(r"<br\s*/?>", "\n", s(h), flags=re.I)
    t = re.sub(r"</(p|h[1-6]|li|figure|blockquote|pre)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        t = t.replace(a, b)
    return t


def parse_detail(j):
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict) or not d.get("key"):
        raise NaError("PARSE", "記事の詳細を読み取れませんでした。")
    body = s(d.get("body"))
    text = re.sub(r"\s+", "", html_to_text(body))
    return {"key": d["key"], "textLength": len(text), "h2Count": len(re.findall(r"<h2[\s>]", body, re.I)), "imageCount": len(re.findall(r"<img[\s>]", body, re.I))}


def parse_likes(j, key):
    d = (j or {}).get("data") if isinstance(j, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("likes"), list):
        raise NaError("PARSE", "スキした人の一覧を読み取れませんでした（noteの仕様が変わった可能性があります）。")
    likes = []
    for l in d["likes"]:
        u = l.get("user") or {}
        ms = parse_time(l.get("created_at"))
        if s(u.get("urlname")) and not u.get("is_me") and ms is not None:
            likes.append({"key": key, "likedMs": ms, "urlname": s(u.get("urlname")), "nickname": s(u.get("nickname"))})
    return {"isLast": bool(j.get("is_last_page")) or len(d["likes"]) == 0, "likes": likes}


CID_RE = re.compile(r"^[A-Za-z0-9_\-]{4,40}$")


def parse_comments(j, key, own=""):
    """コメントした人。本文は読まない・返さない（誰が・いつ・どの記事に、だけ）。
    own（自分の urlname）のコメントは byOwner=True にする（ファンの数には入れない）。"""
    d = j.get("data") if isinstance(j, dict) else None
    if not isinstance(d, list):
        raise NaError("PARSE", "コメントした人の一覧を読み取れませんでした（noteの仕様が変わった可能性があります）。")
    out = []
    for c in d:
        if not isinstance(c, dict) or c.get("is_blocked"):
            continue
        u = c.get("user") or {}
        name, cid, ms = s(u.get("urlname")), s(c.get("key")), parse_time(c.get("created_at"))
        if not ID_RE.match(name) or not CID_RE.match(cid) or ms is None:
            continue
        out.append({"key": key, "cid": cid, "urlname": name, "nickname": s(u.get("nickname")), "commentedMs": ms,
                    "byOwner": bool(own) and name.lower() == own.lower(), "replied": bool(c.get("is_creator_replied"))})
    nxt = j.get("next_page")
    try:
        nxt = int(nxt) if nxt else 0
    except (TypeError, ValueError):
        nxt = 0
    return {"comments": out, "next": nxt, "total": num0(j.get("total_count"))}


# ---------- ログイン中ダッシュボード（未検証：公開されている解説記事にもとづく） ----------
def auth_response_state(code, content_type, body, location=""):
    ct = s(content_type).lower()
    b = s(body)[:200].lower()
    if code in (401, 403):
        return "invalid", f"noteから「ログインしていない／許可されていない」（{code}）と返されました"
    if 300 <= code < 400:
        path = re.sub(r"^https?://[^/]+", "", s(location)).split("?")[0]
        return "invalid", f"ログイン画面などへ転送されました（{code}" + (f" → {url_host(location)}{path}" if location else "") + "）"
    if code == 429:
        return "busy", "noteから「アクセスが多い」（429）と返されました"
    if code >= 500:
        return "error", f"noteのサーバーエラー（{code}）"
    if code == 404:
        return "error", "取得先が見つかりません（404）。noteの仕様が変わった可能性があります"
    if code >= 400:
        return "error", f"noteへのアクセスでエラー（{code}）"
    if "text/html" in ct or re.match(r"^\s*(<!doctype|<html)", b):
        return "invalid", "JSON ではなく Web ページ（HTML）が返されました。ログインが切れている可能性があります"
    return "ok", ""


def _pick(o, names):
    for n in names:
        if isinstance(o, dict) and o.get(n) not in (None, ""):
            return o[n]
    return None


def parse_stats_pv(j):
    d = (j.get("data") or j) if isinstance(j, dict) else None
    lst = _pick(d, ["note_stats", "noteStats", "notes"]) if isinstance(d, dict) else None
    if not isinstance(lst, list):
        raise NaError("PARSE", "ダッシュボード（stats/pv）の応答に記事一覧（note_stats）が見つかりませんでした。noteの仕様が変わった可能性があります。")
    items, skipped = [], 0
    for x in lst:
        key = s(_pick(x, ["key", "note_key", "noteKey"])) or key_from_url(_pick(x, ["note_url", "noteUrl", "url"]))
        pv = _pick(x, ["read_count", "readCount", "pv", "page_view_count", "pageViewCount"])
        try:
            pvn = float(pv)
        except (TypeError, ValueError):
            pvn = None
        if not key or pvn is None:
            skipped += 1
            continue
        lk, cm = _pick(x, ["like_count", "likeCount"]), _pick(x, ["comment_count", "commentCount"])
        items.append({"key": key, "title": s(_pick(x, ["name", "title"])), "pv": int(pvn) if pvn.is_integer() else pvn,
                      "likes": "" if lk is None else num0(lk), "comments": "" if cm is None else num0(cm)})
    if lst and not items:
        raise NaError("PARSE", "ダッシュボード（stats/pv）の記事データに記事キーやビュー数（read_count）が見つかりませんでした。noteの仕様が変わった可能性があります。")
    last = _pick(d, ["last_page", "lastPage", "is_last_page", "isLastPage"])
    return {"items": items, "skipped": skipped, "isLast": last is True or last == "true" or len(lst) == 0}


GQL_QUERY_FULL = ("query NaDashboard($date: Datetime!, $after: String) { dashboardSummary(unit: DAY, date: $date) { lastUpdatedAt metrics { impressionCount pageViewCount } } "
                  "dashboardNoteListConnection(unit: DAY, date: $date, order: PUBLISHED_DATE_DESC, first: 50, after: $after) { pageInfo { hasNextPage endCursor } "
                  "edges { node { note { link { absoluteUrl } } metrics { impressionCount pageViewCount likeCount commentCount salesAmount } } } } }")
GQL_QUERY_MIN = ("query NaDashboard($date: Datetime!, $after: String) { dashboardSummary(unit: DAY, date: $date) { lastUpdatedAt metrics { impressionCount pageViewCount } } "
                 "dashboardNoteListConnection(unit: DAY, date: $date, order: PUBLISHED_DATE_DESC, first: 50, after: $after) { pageInfo { hasNextPage endCursor } "
                 "edges { node { note { link { absoluteUrl } } metrics { impressionCount pageViewCount } } } } }")


def gql_body(date_str, after=None, minimal=False):
    return json.dumps({"query": GQL_QUERY_MIN if minimal else GQL_QUERY_FULL, "variables": {"date": date_str + "T00:00:00.000Z", "after": after}})


def gql_token_from_set_cookie(values):
    tok = ""
    for c in values or []:
        m = re.search(r"(?:^|[;,\s])note_gql_auth_token=([^;,\s]+)", " " + c)
        if m:
            tok = m.group(1)
    return tok


def parse_gql(j):
    if isinstance(j, dict) and isinstance(j.get("errors"), list) and j["errors"]:
        msg = " / ".join(s((e or {}).get("message")) for e in j["errors"][:2] if (e or {}).get("message"))
        err = NaError("GQL", "新ダッシュボードの応答がエラーでした：" + (msg or "理由不明")[:200])
        err.field = bool(re.search(r"cannot query field|unknown (field|argument)|undefined field", msg, re.I))
        raise err
    d = j.get("data") if isinstance(j, dict) else None
    conn = d.get("dashboardNoteListConnection") if isinstance(d, dict) else None
    if not isinstance(conn, dict) or not isinstance(conn.get("edges"), list):
        raise NaError("PARSE", "新ダッシュボードの応答に記事一覧（dashboardNoteListConnection）が見つかりませんでした。noteの仕様が変わった可能性があります。")
    items, skipped = [], 0
    o = lambda v: "" if v in (None, "") else num0(v)
    for e in conn["edges"]:
        n = (e or {}).get("node") or {}
        note = n.get("note") or {}
        m = n.get("metrics") or {}
        key = key_from_url((note.get("link") or {}).get("absoluteUrl")) or s(note.get("key"))
        if not key:
            skipped += 1
            continue
        items.append({"key": key, "pv": o(m.get("pageViewCount")), "imp": o(m.get("impressionCount")), "likes": o(m.get("likeCount")), "comments": o(m.get("commentCount")), "sales": o(m.get("salesAmount"))})
    sm = ((d.get("dashboardSummary") or {}).get("metrics") or {})
    pi = conn.get("pageInfo") or {}
    all_zero = bool(items) and all(not x["pv"] and not x["imp"] for x in items)
    return {"items": items, "skipped": skipped, "hasNext": bool(pi.get("hasNextPage")) and bool(pi.get("endCursor")), "endCursor": s(pi.get("endCursor")),
            "suspectAnonymous": (not items or all_zero) and not num0(sm.get("pageViewCount")) and not num0(sm.get("impressionCount"))}
