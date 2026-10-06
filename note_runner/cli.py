"""実行の入口。GitHub Actions: python -m note_runner ／ Colab: note_runner.cli.colab_main()
秘密（Cookie・合言葉・受け取りURL）は環境変数か getpass でだけ受け取り、画面・ログ・ファイルには出しません。"""
import argparse
import re
import json
import os
import sys
import time
import uuid

from . import VERSION, core, fetch
from .httpc import Client
from .sender import Sender, wire_len

MAX_CHUNK = 1_000_000   # 1回に送る大きさ。実際に送る封筒の JSON（日本語は \\uXXXX にエスケープ後）の文字数で数える
RECEIVER_MAX = 1_500_000   # 受け取り側（Apps Script）の上限：封筒の JSON 全体の文字数
# 1回に送る件数の上限（受け取り側の NA_RX_LIMITS 以下。pv は書き込みが時間切れにならないよう少なめ）。v1.4.2：さかのぼりで pv が 2000 件を超えて止まったため
COMMENTERS_FULL_EXTRA = 300   # コメントの取り直し（手動）の分だけ、アクセスの上限を足す   # 手動でさかのぼる日数を指定したときの、さかのぼりのアクセス回数の上限（3秒間隔で約18分）
ITEM_MAX = {"articles": 3000, "snaps": 4000, "likers": 8000, "commenters": 3000, "pv": 1000}


class Redactor:
    """ログに秘密が混ざっても伏せ字にする（念のための二重の対策）"""
    def __init__(self, secrets, stream=None):
        self.secrets = [x for x in secrets if x and len(x) >= 8]
        self.stream = stream or sys.stdout
        self.lines = []

    def clean(self, msg):
        t = str(msg)
        for x in self.secrets:
            t = t.replace(x, core.mask(x))
        return t

    def __call__(self, msg):
        t = self.clean(msg)
        self.lines.append(t)
        print(t, file=self.stream, flush=True)


REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")


def repo_of(env):
    """GitHub Actions で動いているときだけ「持ち主/名前」（シートのトークン登録の初期値に使う）。形が違えば送らない"""
    if (env or {}).get("GITHUB_ACTIONS") != "true":
        return ""
    r = (env.get("GITHUB_REPOSITORY") or "").strip()
    return r if REPO_RE.fullmatch(r) else ""


def chunk_payloads(out, run_id, source, started_ms, requests, message, has_cookie=False, repo=""):
    D = out["dash"]
    # PV をスキップしても Cookie があれば authState=ok。受け取り側は skipped のとき前回 cookie を引き継ぐため、
    # 以前 none のままだと「NOTE_SESSIONがありません」が残り続ける。
    auth = {"ok": "ok", "invalid": "invalid", "none": "none"}.get(D["state"], "skipped")
    if auth == "skipped" and has_cookie:
        auth = "ok"
    log = {"result": "中断" if out["stopped"] else ("一部エラー" if out["errors"] else "成功"), "requests": requests, "message": message, "startedAt": started_ms,
           "authState": auth, "authReason": D["reason"] if D["state"] == "invalid" else "", "dashOk": D["state"] == "ok"}
    base = lambda: {"kind": "note-analytics", "action": "data", "runId": run_id, "source": source}
    bodies = []
    for sec in ("articles", "snaps", "likers", "commenters", "pv"):
        cur, size = [], 0
        for item in out[sec]:
            n = wire_len(item) + 1   # エスケープ後の長さ（日本語1文字＝6文字）。エスケープ前で数えると上限を超えることがある
            if cur and (size + n > MAX_CHUNK or len(cur) >= ITEM_MAX[sec]):
                b = base(); b[sec] = cur; bodies.append(b); cur, size = [], 0
            cur.append(item); size += n
        if cur:
            b = base(); b[sec] = cur; bodies.append(b)
    fin = base()
    fin.update({"profiles": out["profiles"], "details": out["details"], "likersAt": out["likersAt"], "likersChecked": out["likersChecked"],
                "commentersAt": out["commentersAt"], "commentersChecked": out["commentersChecked"], "log": log, "final": True})
    if repo and REPO_RE.fullmatch(repo):
        fin["repo"] = repo
    bodies.append(fin)
    for i, b in enumerate(bodies):
        b["chunk"], b["chunks"] = i + 1, len(bodies)
    return bodies


def too_large(bodies):
    """受け取り側の上限を超える回があれば、その番号（1から）。封筒の他の項目（署名など）の分として 1,000 文字の余裕をみる"""
    for i, b in enumerate(bodies):
        if wire_len(b) + 1000 > RECEIVER_MAX:
            return i + 1
    return 0


def run(env, argv=None, source="github", log_stream=None, opener=None, sender=None, sleep=time.sleep, now_ms=None):
    ap = argparse.ArgumentParser(prog="note_runner")
    ap.add_argument("--dry-run", action="store_true", help="シートに送らず、取得結果をファイルに書く（--out）")
    ap.add_argument("--out", default=os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "note-runner-dry-run.json"))
    ap.add_argument("--no-dashboard", action="store_true", help="Cookie があっても PV の自動取得をしない")
    a = ap.parse_args(argv or [])

    cookie_raw = env.get("NOTE_SESSION") or ""
    cookie, why = (None, "") if not cookie_raw else core.normalize_cookie(cookie_raw)
    url, secret = env.get("NA_RECEIVER_URL") or "", env.get("NA_RECEIVER_SECRET") or ""
    secrets_list = [cookie_raw, cookie or "", secret, url.split("/s/")[-1].split("/")[0] if "/s/" in url else ""]
    if env.get("GITHUB_ACTIONS") == "true":
        for x in secrets_list:
            if x:
                print(f"::add-mask::{x}", file=log_stream or sys.stdout, flush=True)   # GitHub のログで自動的に *** に置き換えさせる
    log = Redactor(secrets_list, log_stream)
    log(f"note分析ランナー v{VERSION}（{source}）")
    if cookie_raw and not cookie:
        log("⚠ NOTE_SESSION の形が想定と違うので、PVの自動取得はしません：" + why)
    if a.no_dashboard:
        cookie = None

    out_path = os.path.abspath(a.out)
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if a.dry_run and (out_path == here or out_path.startswith(here + os.sep)):
        log("✗ 取得データをリポジトリの中には書きません（--out をリポジトリの外にしてください）。")
        return 1, log
    snd = sender
    state = {}
    if not a.dry_run or (url and secret):   # お試しでも、受け取り先があれば設定だけ読む（書き込みはしない）
        try:
            snd = snd or Sender(url, secret, opener=opener, sleep=sleep, log=log)
            state = snd.get_state()
        except core.NaError as e:
            log("✗ シートの受け取り先に接続できませんでした：" + e.message)
            return 1, log
        # no_dashboard のときは Cookie をわざと使わないので「なし」とは書かない（誤解を防ぐ）
        cookie_txt = ('あり ' + core.mask(cookie)) if cookie else ('この実行では使わない（--no-dashboard）' if a.no_dashboard else 'なし')
        log(f"シートの設定を読みました：自分={state.get('own') or '（なし）'}／ベンチマーク={','.join(state.get('bench') or []) or '（なし）'}／Cookie={cookie_txt}／シート v{str(state.get('version') or '?')[:20]}")
    P = fetch.plan_settings(state, env.get("NOTE_OWN", ""), [b for b in (env.get("NOTE_BENCH") or "").split(",") if b])
    # v1.4.3：手動実行のときだけ「1回でさかのぼる日数」を上書きできる（GitHub の入力 imp_backfill → NA_IMP_BACKFILL。0〜120。空ならシートの設定）
    if str(env.get("NA_LIKERS_FULL") or "").strip().lower() in ("1", "true", "yes"):
        P["likersFull"] = True
        log("スキした人：スキが多い記事（200超）は、この実行で最初のページから取り直します（手動の指定）")
    if str(env.get("NA_COMMENTERS_FULL") or "").strip().lower() in ("1", "true", "yes"):
        P["commentersFull"] = True
        log("コメントした人：コメントが30件を超える記事は、この実行で最初のページから取り直します（手動の指定）")
    ov = str(env.get("NA_IMP_BACKFILL") or "").strip()
    if ov.isdigit():
        P["impBackfill"] = max(0, min(core.IMP_MAX_BACK_DAYS, int(ov)))
        log(f"インプレッションのさかのぼり：この実行では1回 {P['impBackfill']} 日分まで（手動の指定・アクセス {fetch.imp_budget(P['impBackfill'])} 回まで）")
    if not P["own"] and not P["bench"]:
        log("✗ 自分のクリエイターIDもベンチマークもありません（シートの「設定」を確認してください）。")
        return 1, log
    started = int(now_ms if now_ms is not None else time.time() * 1000)
    tl = str(env.get("NA_TIME_LIMIT_MIN") or "").strip()   # v1.4.5：GitHub の実行時間の上限（分）。この時間を過ぎたら、さかのぼりを止めて取れた分を送る
    if tl.isdigit() and int(tl) > 0:
        P["deadline"] = time.time() + int(tl) * 60
        log(f"実行時間の上限：{tl} 分を過ぎたら、さかのぼりを止めて取れた分を送ります（のこりは次回）")
    P["impBudget"] = fetch.imp_budget(P.get("impBackfill") or 0)   # v1.4.5：手動でも毎日の実行でも、さかのぼる日数に合わせる（1日分＝1〜3回・間隔3秒以上は変えない）
    http = Client(cookie_value=cookie, interval_sec=P["interval"], sleep=sleep, opener=opener, max_requests=300 + max(0, P.get("impBudget", fetch.IMP_REQ_BUDGET) - fetch.IMP_REQ_BUDGET) + (COMMENTERS_FULL_EXTRA if P.get("commentersFull") else 0) + max(0, P["benchPages"] - 3) * len(P["bench"]))
    out = fetch.run(http, P, started, log, has_cookie=bool(cookie), dash_off=bool(a.no_dashboard))
    msg = fetch.summary_message(out, http.requests)
    bodies = chunk_payloads(out, uuid.uuid4().hex[:12], source, started, http.requests, msg, has_cookie=bool(cookie), repo=repo_of(env))
    # 念のため：送るデータに Cookie・トークンが絶対に入っていないこと
    blob = json.dumps(bodies, ensure_ascii=False)
    for x in [cookie_raw, cookie, http.token, secret]:
        if x and x in blob:
            log("✗ 安全のため止めました：送るデータに秘密の文字列が入っていました。")
            return 1, log
    big = too_large(bodies)
    if big:
        log(f"✗ 送るデータ（{big}回目）が受け取り側の上限（{RECEIVER_MAX:,} 文字）を超えるので、送らずに止めました。")
        return 1, log
    if a.dry_run:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bodies, f, ensure_ascii=False)
        log(f"（お試し）シートには送らず {out_path} に書きました（{len(bodies)} 回分）。")
    else:
        try:
            for b in bodies:
                snd.send_data(b)
        except core.NaError as e:
            log("✗ シートへの書き込みで止まりました：" + e.message)
            return 1, log
        again = getattr(snd, "retries", 0)
        log(f"シートに送りました（{len(bodies)} 回に分けて）。" + (f"（途中で {again} 回送り直しました）" if again else ""))
    log(msg)
    if out["stopped"]:
        log(("::warning::" if env.get("GITHUB_ACTIONS") == "true" else "⚠ ") + "noteへのアクセスを途中で止めました：" + out["stopped"])
    if out["dash"]["state"] == "invalid":
        log(("::error::" if env.get("GITHUB_ACTIONS") == "true" else "✗ ") + "note の Cookie が使えなくなりました。Secrets の NOTE_SESSION を新しい値にしてください。")
        return 1, log
    return 0, log


def main():
    code, _ = run(dict(os.environ), sys.argv[1:], source=os.environ.get("NA_SOURCE", "github"))
    sys.exit(code)


def colab_main(dry_run=False, use_cookie=True):
    """Colab 用。入力はすべて getpass（画面にもノートブックにも残らない）"""
    from getpass import getpass
    env = {}
    env["NA_RECEIVER_URL"] = input("受け取り用ウェブアプリのURL（https://script.google.com/macros/s/…/exec）: ").strip() if not dry_run else ""
    env["NA_RECEIVER_SECRET"] = getpass("受け取り用の合言葉（入力しても表示されません）: ").strip() if not dry_run else ""
    if use_cookie:
        env["NOTE_SESSION"] = getpass("note の _note_session_v5 の値（使わないなら空のまま Enter）: ").strip()
    if dry_run:
        env["NOTE_OWN"] = input("自分のクリエイターID: ").strip()
    try:
        code, _ = run(env, ["--dry-run"] if dry_run else [], source="colab")
    finally:
        env.clear()   # 入力した値をすぐ消す
    return code
