"""
新高値ブレイク投資法に基づく銘柄スクリーニング → Discord通知

投資法（投資法/☆新高値ブレイク投資とは.txt に基づく）:
  - 過去52週の高値を当日終値がブレイクしていること
  - PERが10~30倍（赤字から黒字転換した場合は除外しない）
  - 1株あたり単価は500円以下（CLAUDE.md の運用方針）
  - 東証プライム・スタンダード・グロースの普通株式のみが対象

データソース: J-Quants API v2 (https://jpx-jquants.com/)
通知先: Discord Webhook

必須環境変数:
  JQUANTS_API_KEY      J-Quants API v2 のAPIキー
  DISCORD_WEBHOOK_URL  Discord Webhook URL
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

API_BASE = "https://api.jquants.com/v2"
API_KEY = os.environ["JQUANTS_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

STRATEGY_NAME = "新高値ブレイク投資"  # 投資法/☆新高値ブレイク投資とは.txt に対応
MAX_PRICE = 500.0
PER_MIN = 10.0
PER_MAX = 30.0
LOOKBACK_CALENDAR_DAYS = int(os.environ.get("LOOKBACK_CALENDAR_DAYS", "380"))  # 52週+バッファ
TARGET_MARKETS = {"0111", "0112", "0113"}  # プライム・スタンダード・グロース
REQUEST_INTERVAL_SEC = 1.1  # Lightプラン(60req/分)に対する安全マージン


def api_get(path, params):
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20)
                continue
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"J-Quants API error {e.code} for {path}: {body}")
        except (http.client.RemoteDisconnected, ConnectionError, urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(3)
    raise RuntimeError(f"J-Quants API retry exceeded for {path}")


def get_trading_days(from_date, to_date):
    data = api_get("/markets/calendar", {
        "from": from_date.strftime("%Y%m%d"),
        "to": to_date.strftime("%Y%m%d"),
    })
    return sorted(d["Date"].replace("-", "") for d in data["data"] if d["HolDiv"] == "1")


def get_listed_master():
    data = api_get("/equities/master", {})
    universe = {}
    for row in data["data"]:
        if row.get("Mkt") in TARGET_MARKETS:
            universe[row["Code"]] = row
    return universe


def collect_daily_bars(trading_days):
    """銘柄コード -> [(date, close), ...] (日付昇順)"""
    series = {}
    for i, day in enumerate(trading_days):
        data = api_get("/equities/bars/daily", {"date": day})
        for row in data["data"]:
            code = row["Code"]
            close = row.get("AdjC") or row.get("C")
            if close is None:
                continue
            series.setdefault(code, []).append((day, close))
        if i < len(trading_days) - 1:
            time.sleep(REQUEST_INTERVAL_SEC)
    return series


def find_new_high_breakouts(series, universe):
    candidates = []
    for code, bars in series.items():
        if code not in universe:
            continue
        if len(bars) < 20:
            continue
        bars_sorted = sorted(bars, key=lambda x: x[0])
        latest_date, latest_close = bars_sorted[-1]
        if latest_close > MAX_PRICE:
            continue
        prior_high = max(c for _, c in bars_sorted[:-1])
        if latest_close > prior_high:
            candidates.append({
                "code": code,
                "name": universe[code].get("CoName", ""),
                "close": latest_close,
                "prior_52w_high": prior_high,
                "date": latest_date,
            })
    return candidates


def get_latest_eps(code):
    data = api_get("/fins/summary", {"code": code})
    rows = [r for r in data["data"] if r.get("EPS") not in (None, "", "0")]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("DiscDate", ""), r.get("DiscNo", "")))
    try:
        return float(rows[-1]["EPS"])
    except (TypeError, ValueError):
        return None


def apply_per_filter(candidates):
    results = []
    for c in candidates:
        eps = get_latest_eps(c["code"])
        time.sleep(REQUEST_INTERVAL_SEC)
        if eps is None or eps <= 0:
            continue  # 赤字は新高値ブレイク投資法の対象外(黒字転換の判定は財務詳細APIが必要なため今回は除外)
        per = c["close"] / eps
        if PER_MIN <= per <= PER_MAX:
            c["per"] = round(per, 1)
            c["eps"] = eps
            results.append(c)
    return results


def notify_discord(candidates):
    if not candidates:
        content = f"{STRATEGY_NAME}に合う銘柄が今回は見つかりませんでした"
    else:
        lines = [f"**{STRATEGY_NAME} - 本日の候補銘柄**"]
        for c in candidates:
            lines.append(
                f"- {c['name']}（{c['code']}）"
                f"　終値: {c['close']:.0f}円"
                f"　52週高値: {c['prior_52w_high']:.0f}円"
                f"　PER: {c['per']}倍"
            )
        content = "\n".join(lines)

    body = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; kabu-screener/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()


def main():
    today = datetime.now()
    from_date = today - timedelta(days=LOOKBACK_CALENDAR_DAYS)

    print("上場銘柄マスタを取得中...")
    universe = get_listed_master()
    print(f"対象銘柄数: {len(universe)}")

    print("取引カレンダーを取得中...")
    trading_days = get_trading_days(from_date, today)
    print(f"対象営業日数: {len(trading_days)}")

    print("日次株価を取得中...")
    series = collect_daily_bars(trading_days)

    print("新高値ブレイク銘柄を抽出中...")
    candidates = find_new_high_breakouts(series, universe)
    print(f"1次候補: {len(candidates)}件")

    print("PERフィルタを適用中...")
    results = apply_per_filter(candidates)
    print(f"最終候補: {len(results)}件")

    print("Discordに通知中...")
    notify_discord(results)
    print("完了")


if __name__ == "__main__":
    main()
