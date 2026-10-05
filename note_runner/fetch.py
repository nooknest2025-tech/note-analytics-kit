"""1日1回の取得。順番は Apps Script 版と同じ：
記事一覧・フォロワー → ベンチマーク → ダッシュボード（Cookie があるときだけ・1日1回）→ 本文の文字数 → 誰からのスキ → コメントした人（最後）。
403/429 が返ったら、その時点で note へのアクセスをやめ、取れた分と理由を記録する。"""
import json

from . import core
from .httpc import Stop


def _int(v, default, lo, hi):
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        x = default
    return max(lo, min(hi, x))


def plan_settings(state, env_own="", env_bench=None):
    st = (state or {}).get("settings") or {}
    own = core.normalize_id((state or {}).get("own") or env_own)
    bench = [b for b in ((state or {}).get("bench") or env_bench or []) if core.normalize_id(b) and b != own][:5]
    return {
        "own": own, "bench": bench,
        "interval": max(3.0, float(st.get("intervalSec") or 3)),
        "ownPages": _int(st.get("ownPages") or 300, 300, 1, 300),
        "benchPages": _int(st.get("benchPages") or 3, 3, 1, 10),
        "detailMode": st.get("detailMode") if st.get("detailMode") in ("自分だけ", "すべて", "しない") else "自分だけ",
        "detailLimit": _int(st.get("detailLimit") if st.get("detailLimit") is not None else 30, 30, 0, 100),
        "snapDays": _int(st.get("snapDays") or 30, 30, 1, 3650),
        "likers": st.get("likers", True) is not False,
        "likerPages": _int(st.get("likerPages") or 5, 5, 1, 5),
        "likerArticles": 30,
        # コメントした人：シート側が v1.3.0 以上（設定に commenters がある）ときだけ。古いシートは受け取れないので取りに行かない
        "commenters": "commenters" in st and st.get("commenters") is not False,
        "commenterArticles": 20, "commenterPages": 3,
        "dash": bool(st.get("dash")), "dashImp": st.get("dashImp", True) is not False,
        "dashPages": _int(st.get("dashPages") or 30, 30, 1, 50),
        "dashShareOverride": bool(st.get("dashShareOverride")),
        "shared": bool(((state or {}).get("sharing") or {}).get("shared")),
        "lastDashDate": (state or {}).get("lastDashDate") or "",
        "known": {r[0]: {"likes": r[1], "likersAt": r[2] or 0, "hasText": bool(r[3]), "lastLike": r[4] or 0, "publishMs": r[5],
                         "commentersAt": (r[6] if len(r) > 6 else 0) or 0} for r in ((state or {}).get("ownArticles") or []) if r and r[0]},
    }


def run(http, P, now_ms, log, has_cookie=False, dash_off=False):
    out = {"profiles": [], "articles": [], "details": [], "snaps": [], "likers": [], "likersAt": [], "commenters": [], "commentersAt": [], "pv": [], "errors": [], "stopped": "", "stopHttp": None,
           "likersChecked": False, "commentersChecked": False, "dash": {"state": "none", "reason": "", "pvRows": 0, "dailyRows": 0, "notes": []}}
    today = core.jst(now_ms)["date"]
    arts = {}

    def err(msg):
        if len(out["errors"]) < 20:
            out["errors"].append(msg)

    def task(name, fn):
        if out["stopped"]:
            return
        try:
            fn()
        except Stop as e:
            out["stopped"] = f"「{name}」の取得中に：{e.message}"
            out["stopHttp"] = e.http
            log(f"止めました：{out['stopped']}")
        except core.NaError as e:
            err(f"{name}: {e.message}")
            log(f"エラー：{name}: {e.message}")

    # 1) 記事一覧・フォロワー（自分 → ベンチマーク）
    creators = ([(P["own"], True)] if P["own"] else []) + [(b, False) for b in P["bench"]]
    for cid, own in creators:
        def prof(cid=cid):
            out["profiles"].append(core.parse_creator(json.loads(http.get_public(core.creator_url(cid)))))
        task(f"プロフィール {cid}", prof)

        def lst(cid=cid, own=own):
            mx = P["ownPages"] if own else P["benchPages"]
            for page in range(1, mx + 1):
                L = core.parse_list(json.loads(http.get_public(core.list_url(cid, page))), cid)
                for a in L["articles"]:
                    if a["creator"] != cid:
                        continue   # 共同マガジンなどの他人の記事は入れない
                    arts[a["key"]] = a
                    out["articles"].append(a)
                    age_h = (now_ms - a["publishMs"]) / core.HOUR_MS
                    if own or age_h <= P["snapDays"] * 24:
                        out["snaps"].append({"creator": cid, "key": a["key"], "ageH": round(age_h, 1), "likes": a["likes"], "comments": a["comments"], "t": now_ms})
                if L["isLast"] or not L["articles"]:
                    break
        task(f"記事一覧 {cid}", lst)

    # 2) ログイン中ダッシュボード（自分の Cookie があるときだけ・1日1回）
    D = out["dash"]
    if dash_off:
        D["state"], D["reason"] = "skipped", "この実行では PV を取りませんでした（--no-dashboard）"
    elif not has_cookie:
        D["state"], D["reason"] = "none", "Cookie（NOTE_SESSION）がないので、PVの自動取得はしませんでした"
    elif not P["dash"]:
        D["state"], D["reason"] = "skipped", "シートの設定「PVの自動取得（自分のCookie）」が「いいえ」です"
    elif P["lastDashDate"] == today:
        D["state"], D["reason"] = "skipped", "今日はもう取得しています（1日1回）"
    elif P["shared"] and not P["dashShareOverride"]:
        D["state"], D["reason"] = "skipped", "スプレッドシートが自分以外と共有されているので、Cookie を使う取得はしませんでした"
    elif not P["own"]:
        D["state"], D["reason"] = "skipped", "自分のクリエイターIDがありません"
    else:
        task("PVの自動取得", lambda: _dash(http, P, now_ms, out, arts, log))

    # 3) 本文の文字数（まだ取っていない記事だけ）
    if P["detailMode"] != "しない" and P["detailLimit"] > 0:
        def det():
            cand = [a for a in arts.values() if (a["creator"] == P["own"] or P["detailMode"] == "すべて") and not P["known"].get(a["key"], {}).get("hasText")]
            cand.sort(key=lambda a: -a["publishMs"])
            for a in cand[:P["detailLimit"]]:
                try:
                    out["details"].append(core.parse_detail(json.loads(http.get_public(core.detail_url(a["key"])))))
                except core.NaError as e:
                    err(f"本文 {a['key']}: {e.message}")
        task("本文の文字数", det)

    # 4) 誰からのスキ（自分の記事だけ・最後。日付だけ記録）
    if P["likers"] and P["own"]:
        def lik():
            mine = [a for a in arts.values() if a["creator"] == P["own"] and a["likes"] > (P["known"].get(a["key"], {}).get("likersAt") or 0)]
            mine.sort(key=lambda a: -a["publishMs"])
            seen = set()
            for a in mine[:P["likerArticles"]]:
                since = P["known"].get(a["key"], {}).get("lastLike") or 0   # 前回記録した、いちばん新しいスキの日（0時）
                for page in range(1, P["likerPages"] + 1):
                    R = core.parse_likes(json.loads(http.get_public(core.likes_url(a["key"], page))), a["key"])
                    stop = R["isLast"]
                    for l in R["likes"]:
                        if l["likedMs"] < since:
                            stop = True
                            break
                        k = a["key"] + "|" + l["urlname"]
                        if k in seen:
                            continue
                        seen.add(k)
                        out["likers"].append({"key": a["key"], "urlname": l["urlname"], "nickname": l["nickname"], "day": core.jst(l["likedMs"])["date"]})
                    if stop:
                        break
                out["likersAt"].append({"key": a["key"], "likes": a["likes"]})
                out["likersChecked"] = True
        task("スキした人", lik)

    # 5) コメントした人（自分の記事だけ・スキの後＝いちばん最後。本文は保存しない。日付だけ記録）
    #    前回確認したときよりコメント数が増えた記事だけ。初回はコメントのある記事を新しい順に。1回20記事・1記事3ページまで
    if P["commenters"] and P["own"]:
        def com():
            mine = [a for a in arts.values() if a["creator"] == P["own"] and a["comments"] > (P["known"].get(a["key"], {}).get("commentersAt") or 0)]
            mine.sort(key=lambda a: -a["publishMs"])
            seen = set()
            for a in mine[:P["commenterArticles"]]:
                page = 1
                try:
                    for _ in range(P["commenterPages"]):
                        R = core.parse_comments(json.loads(http.get_public(core.comments_url(a["key"], page))), a["key"], P["own"])
                        for c in R["comments"]:
                            if c["cid"] in seen:
                                continue
                            seen.add(c["cid"])
                            out["commenters"].append({"key": a["key"], "cid": c["cid"], "urlname": c["urlname"], "nickname": c["nickname"],
                                                      "day": core.jst(c["commentedMs"])["date"], "byOwner": c["byOwner"], "replied": c["replied"]})
                        if not R["next"] or R["next"] <= page:
                            break
                        page = R["next"]
                except core.NaError as e:
                    if e.code != "NOTFOUND":
                        raise
                    err(f"コメント {a['key']}: {e.message}")   # 削除された記事など。ほかの記事は続ける
                out["commentersAt"].append({"key": a["key"], "comments": a["comments"]})
                out["commentersChecked"] = True
        task("コメントした人", com)
    return out


def _dash(http, P, now_ms, out, arts, log):
    D = out["dash"]
    today = core.jst(now_ms)["date"]
    yday = core.jst(now_ms - core.DAY_MS)["date"]

    def check(r, kind):
        if r["state"] == "ok":
            return
        if r["state"] == "invalid":
            if kind == "cookie":
                D["state"], D["reason"] = "invalid", r["reason"]
                raise core.NaError("COOKIE_INVALID", "Cookie が使えなくなりました：" + r["reason"])
            raise core.NaError("TOKEN", "新ダッシュボードの一時トークンが受け付けられませんでした：" + r["reason"])
        if r["state"] == "busy":
            raise Stop(429, r["reason"] + "。今日の取得は止めます。")
        raise core.NaError("HTTP", r["reason"])

    for page in range(1, P["dashPages"] + 1):
        r = http.stats_pv(page)
        check(r, "cookie")
        try:
            j = json.loads(r["body"])
        except ValueError:
            raise core.NaError("PARSE", "ダッシュボード（stats/pv）の応答が JSON ではありませんでした。noteの仕様が変わった可能性があります。")
        S = core.parse_stats_pv(j)
        for x in S["items"]:
            a = arts.get(x["key"])
            out["pv"].append({"date": today, "period": "全期間", "key": x["key"], "title": a["title"] if a else x["title"], "pv": x["pv"], "imp": "", "likes": x["likes"], "comments": x["comments"], "sales": "", "method": core.METHOD_STATS})
            D["pvRows"] += 1
        if S["isLast"] or not S["items"]:
            break
        if page == P["dashPages"]:
            D["notes"].append(f"stats/pv：最大ページ数（{P['dashPages']}）で止めました")
    D["state"] = "ok"
    if not P["dashImp"]:
        return
    try:
        r = http.gql_auth()
        check(r, "cookie")
        if not http.token:
            raise core.NaError("GQL", "新ダッシュボード用の一時トークンが返されませんでした（noteの仕様が変わった可能性があります）。インプレッションは取得できません。")
        after, minimal = None, False
        for page in range(1, min(10, P["dashPages"]) + 1):
            r = http.gql(core.gql_body(yday, after, minimal))
            check(r, "bearer")
            try:
                Q = core.parse_gql(json.loads(r["body"]))
            except core.NaError as e:
                if e.code == "GQL" and not minimal:
                    minimal = True
                    D["notes"].append("新ダッシュボード：スキ・コメント・売上の項目が使えなかったので、PV・インプレッションだけ取得します")
                    r = http.gql(core.gql_body(yday, after, True))
                    check(r, "bearer")
                    Q = core.parse_gql(json.loads(r["body"]))
                else:
                    raise
            except ValueError:
                raise core.NaError("PARSE", "新ダッシュボードの応答が JSON ではありませんでした。")
            if page == 1 and Q["suspectAnonymous"]:
                D["notes"].append("新ダッシュボード：数字がすべて0でした（ログインが通っていない可能性）。記録しませんでした")
                return
            for x in Q["items"]:
                a = arts.get(x["key"])
                out["pv"].append({"date": yday, "period": "日次", "key": x["key"], "title": a["title"] if a else "", "pv": x["pv"], "imp": x["imp"], "likes": x["likes"], "comments": x["comments"], "sales": x["sales"], "method": core.METHOD_GQL})
                D["dailyRows"] += 1
            if not Q["hasNext"]:
                break
            after = Q["endCursor"]
    finally:
        http.token = None   # 一時トークンはすぐ捨てる


def summary_message(out, requests):
    D = out["dash"]
    msg = f"記事 {len(out['articles'])} 件（noteへのアクセス {requests} 回）。"
    if out["stopped"]:
        msg = "途中で止めました：" + out["stopped"] + "\n" + msg
    msg += "\nPVの自動取得：" + ({"ok": f"全期間 {D['pvRows']} 記事" + (f"／前日分 {D['dailyRows']} 記事" if D["dailyRows"] else "")}.get(D["state"]) or (D["reason"] or D["state"]))
    if D["notes"]:
        msg += "\n- " + "\n- ".join(D["notes"])
    if out["likersChecked"]:
        msg += f"\n新しく記録したスキ: {len(out['likers'])} 件"
    if out["commentersChecked"]:
        msg += f"\nコメントを確認した記事: {len(out['commentersAt'])} 本（コメント {len(out['commenters'])} 件）"
    if out["errors"]:
        msg += "\nうまくいかなかったもの：\n- " + "\n- ".join(out["errors"][:5])
    return msg
