#!/usr/bin/env python3
"""
gmgn-wallet-analysis — the four-gate wallet dossier.

Answers one question: "this wallet's numbers look good — should I actually copy it,
and if I do, what happens to me?"

Four gates, each pass/fail, each with the number that decided it:
  G1 AUTHENTICITY  is the record real, or one lucky coin / a dev marking its own homework?
  G2 CURRENCY      is the edge still working THIS week, or is the good number historical?
  G3 REACHABILITY  can you actually get filled — copy window, entry band, trade size?
  G4 SURVIVABILITY does it cut losses, or does it ride things to zero and take you along?

Verdict is a function of the gates, not a black-box score.

Usage (live):
    python3 analyze.py <wallet> <chain> [zh|en] [--latency <sec>] [--size <usd>]
Usage (offline, for verification):
    python3 analyze.py --fixture fixtures/<name>.json [zh|en]

Read-only. Never signs, never trades.
"""

import json
import os
import statistics
import subprocess
import sys
import time

# ─────────────────────────── plumbing ───────────────────────────

ZH = True


def _(zh, en):
    return zh if ZH else en


def f(v, default=0.0):
    """Every numeric field in this API arrives as a JSON string. Never compare raw."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def i(v, default=0):
    return int(f(v, default))


def _b(v):
    """API booleans arrive as real bools, 0/1, or "true"/"false" strings."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


def pct(x, digits=1):
    return f"{x * 100:.{digits}f}%"


def usd(v):
    v = f(v)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}K"
    if a >= 10:
        return f"{sign}${a:,.0f}"
    return f"{sign}${a:.2f}"


def mc(v):
    v = f(v)
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def dur(sec):
    sec = f(sec)
    if sec <= 0:
        return _("未知", "unknown")
    if sec < 60:
        return f"{sec:.0f}{_(' 秒', 's')}"
    if sec < 3600:
        return f"{sec / 60:.0f}{_(' 分', 'm')}"
    if sec < 86400:
        return f"{sec / 3600:.1f}{_(' 小时', 'h')}"
    return f"{sec / 86400:.1f}{_(' 天', 'd')}"


def med(xs):
    return statistics.median(xs) if xs else 0.0


def dwidth(s):
    """Display width: CJK glyphs occupy two terminal columns."""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def wpad(s, width):
    return s + " " * max(0, width - dwidth(s))


COL = 76           # every emitted line stays inside this display width
BREAK_AFTER = set(" ·，、；。）)]}")


def wrap(text, width):
    """Greedy wrap by display width, breaking after a separator where possible.

    Written by hand rather than with textwrap because textwrap counts characters, and a
    line of CJK is twice as wide as its length — the reason the G3 reason line rendered
    at 231 columns before this existed.
    """
    lines, cur, curw, brk = [], "", 0, -1
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if curw + w > width and cur:
            if 0 <= brk < len(cur) - 1:
                lines.append(cur[:brk + 1].rstrip())
                cur = cur[brk + 1:]
                curw = dwidth(cur)
            else:
                lines.append(cur)
                cur, curw = "", 0
            brk = -1
        cur += ch
        curw += w
        if ch in BREAK_AFTER:
            brk = len(cur) - 1
    if cur:
        lines.append(cur)
    return lines or [""]


def put(out, prefix, text, hang=None):
    """Append `prefix + text`, wrapped, with continuation lines hanging under the text."""
    ind = " " * (dwidth(prefix) if hang is None else hang)
    body = wrap(str(text), COL - dwidth(prefix))
    out.append(prefix + body[0])
    for extra in body[1:]:
        out.append(ind + extra)


def quantile(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def safe_div(a, b, default=0.0):
    return a / b if b else default


# ─── GMGN wallet tags ────────────────────────────────────────────
# `common.tags` is third-party data. Known tags get a meaning and a severity; anything
# unrecognised is printed verbatim and treated as neutral — never silently dropped, and
# never allowed to change control flow.
#   veto_g1 — the P&L itself cannot be trusted
#   veto_g3 — you structurally cannot capture this wallet's edge
#   warn    — changes how you read the numbers
#   good    — a positive signal, still not a reason to skip a gate
TAGS = {
    "wash_trader": ("🚩", "veto_g1", "刷量/对敲交易者", "wash trader",
                    "盈亏可能来自自我对敲，不是市场收益", "P&L may be self-dealt, not market-earned"),
    "sandwich_bot": ("🥪", "veto_g3", "三明治夹子", "sandwich bot",
                     "它的收益来自夹你这类订单", "its profit comes from sandwiching orders like yours"),
    "mev_bot": ("🥪", "veto_g3", "MEV 机器人", "MEV bot",
                "收益来自排序权，不是选币", "profit comes from ordering power, not token selection"),
    "rat_trader": ("🐀", "warn", "老鼠仓", "rat trader",
                   "常见于提前埋伏自己人的盘", "typically front-runs launches it is close to"),
    "bundler": ("📦", "warn", "打包买入", "bundler",
                "与发币方同区块建仓", "builds its position in the launch block"),
    "sniper": ("🎯", "warn", "狙击", "sniper",
               "极早入场，你拿不到同价", "enters far too early for you to match its price"),
    "insider": ("🕵️", "warn", "内幕关联", "insider",
                "信息优势不可复制", "an information edge you cannot replicate"),
    "dev": ("🏭", "warn", "发币方", "token creator",
            "自己发币自己交易", "trades tokens it launched itself"),
    "kol": ("📣", "warn", "KOL", "KOL",
            "喊单者，你大概不是第一个进的", "a caller — you are probably not the first one in"),
    "top_followed": ("👥", "warn", "被大量跟单", "heavily followed",
                     "跟单盘已经推过价，你的滑点更差", "copy flow already moved the price; your slippage is worse"),
    "top_renamed": ("🎭", "warn", "多次改名", "renamed repeatedly",
                    "身份在洗，历史声誉不可延续", "identity keeps churning; past reputation does not carry"),
    "fresh_wallet": ("🆕", "warn", "新钱包", "fresh wallet",
                     "没有可供检验的历史", "no history to check"),
    "smart_money": ("⭐", "good", "聪明钱", "smart money",
                    "GMGN 官方正向标记", "GMGN's own positive marker"),
    "bluechip_owner": ("💎", "good", "蓝筹持有者", "bluechip holder",
                       "持有过存活下来的资产", "has held assets that survived"),
    "whale": ("🐋", "neutral", "巨鲸", "whale",
              "规模远超你，行为不可照搬", "operates at a size that does not transfer to you"),
    "gmgn": ("🔧", "neutral", "GMGN 用户", "GMGN user",
             "通过 GMGN 下单，无风险含义", "trades through GMGN — no risk meaning"),
    "photon": ("🔧", "neutral", "Photon 用户", "Photon user", "下单渠道", "order channel"),
    "bullx": ("🔧", "neutral", "BullX 用户", "BullX user", "下单渠道", "order channel"),
    "maestro": ("🔧", "neutral", "Maestro 用户", "Maestro bot user", "下单渠道", "order channel"),
    "pepeboost": ("🔧", "neutral", "PepeBoost 用户", "PepeBoost user", "下单渠道", "order channel"),
}


def read_tags(raw_tags):
    """Return [{key, emoji, sev, name, meaning, known}] — unknown tags kept verbatim."""
    out = []
    for t in raw_tags or []:
        key = str(t).strip()
        row = TAGS.get(key.lower())
        if row:
            emoji, sev, zh, en, zh_m, en_m = row
            out.append({"key": key, "emoji": emoji, "sev": sev,
                        "name": _(zh, en), "meaning": _(zh_m, en_m), "known": True})
        else:
            out.append({"key": key, "emoji": "❔", "sev": "neutral",
                        "name": f"`{key}`",
                        "meaning": _("未知标签，原样显示，未参与判定",
                                     "unrecognised tag, shown verbatim, not used in any gate"),
                        "known": False})
    return out


# ─────────────────────────── collection ───────────────────────────


class Gap(Exception):
    pass


def cli(args, timeout=45):
    r = subprocess.run(
        ["gmgn-cli"] + args + ["--raw"], capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise Gap((r.stderr or r.stdout or "gmgn-cli failed").strip()[:400])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise Gap("non-JSON response from gmgn-cli")


def unwrap(resp):
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp


def first_row(resp):
    """stats / profits come back as an object, a list, or {list:[...]}, depending on route."""
    d = unwrap(resp)
    if isinstance(d, dict) and isinstance(d.get("list"), list):
        return d["list"][0] if d["list"] else {}
    if isinstance(d, list):
        return d[0] if d else {}
    return d if isinstance(d, dict) else {}


def collect(chain, wallet, gaps):
    """Tiered pull. Tier 1 is mandatory; everything else degrades into `gaps`."""
    d = {}

    # Tier 1 — 4 calls, weight 12. The verdict cannot be issued without these.
    d["stats_7d"] = first_row(
        cli(["portfolio", "stats", "--chain", chain, "--wallet", wallet, "--period", "7d"])
    )
    for key, args in (
        ("stats_30d", ["portfolio", "stats", "--period", "30d"]),
        ("profits_1d", ["portfolio", "profits", "--period", "1d"]),
        ("profits_all", ["portfolio", "profits", "--period", "all"]),
    ):
        try:
            d[key] = first_row(cli(args[:2] + ["--chain", chain, "--wallet", wallet] + args[2:]))
        except Gap as e:
            d[key] = {}
            gaps.append(f"{key}: {e}")

    # Tier 2 — behaviour. activity is the only source of copy-window and entry band.
    acts, cursor = [], None
    for _page in range(3):
        args = ["portfolio", "activity", "--chain", chain, "--wallet", wallet, "--limit", "100"]
        if cursor:
            args += ["--cursor", str(cursor)]
        try:
            raw = unwrap(cli(args))
        except Gap as e:
            gaps.append(f"activity: {e}")
            break
        page = raw.get("activities") or []
        acts += page
        cursor = raw.get("next")
        if not page or not cursor:
            break
    d["activity"] = acts
    if not acts:
        gaps.append(
            _(
                "activity 为空 —— 可跟窗口、入场市值带、加仓/出货姿势本次均未评估",
                "activity empty — copy window, entry band and scale-in/out shape were not evaluated",
            )
        )

    # holdings is CRITICAL auth (needs GMGN_PRIVATE_KEY). Absent key is the normal case.
    # `--sell-out` is documented but rejected by gmgn-cli 1.5.8 ("unknown option"), so it
    # is not passed. The response array is `list`; `holdings` is kept only as a fallback in
    # case a future version renames it to match the docs.
    try:
        raw_h = unwrap(cli(["portfolio", "holdings", "--chain", chain, "--wallet", wallet,
                            "--limit", "50", "--order-by", "total_profit", "--direction", "desc"]))
        d["holdings"] = raw_h.get("list") or raw_h.get("holdings") or []
        if not d["holdings"]:
            gaps.append(_("holdings 返回空 —— 当前持仓、利润集中度、蜜罐检查均未评估",
                          "holdings came back empty — live book, profit concentration and the "
                          "honeypot check were all skipped"))
    except Gap as e:
        d["holdings"] = []
        gaps.append(
            _(f"holdings 不可用（需要 GMGN_PRIVATE_KEY 的 critical auth）：{e} —— "
              "利润集中度改用盈亏桶推断，当前持仓与蜜罐检查缺失",
              f"holdings unavailable (needs GMGN_PRIVATE_KEY / critical auth): {e} — profit "
              "concentration falls back to bucket inference; live book and honeypot check missing")
        )

    # Tier 3 — only when the wallet looks like a launcher.
    common = d["stats_7d"].get("common") or {}
    pnl = d["stats_7d"].get("pnl_stat") or {}
    created = i(common.get("created_token_count"))
    if created > 0 and created > 0.5 * max(1, i(pnl.get("token_num"))):
        try:
            d["created_tokens"] = unwrap(
                cli(["portfolio", "created-tokens", "--chain", chain, "--wallet", wallet])
            )
        except Gap as e:
            d["created_tokens"] = {}
            gaps.append(f"created-tokens: {e}")
    return d


# ─────────────────────────── metrics ───────────────────────────


def ev_type(a):
    return str(a.get("event_type") or a.get("type") or "").lower()


def tok_addr(a):
    t = a.get("token") or {}
    return t.get("address") or t.get("token_address")


def h_get(row, *names):
    """First present field among `names` — the holdings schema differs from the docs."""
    for nm in names:
        if row.get(nm) is not None:
            return row[nm]
    return None


def window_roi(row, cost_key, profit_key):
    """ROI for a window = realized profit / the cost that produced it."""
    cost = f(row.get(cost_key))
    prof = f(row.get(profit_key))
    if cost <= 0:
        return None
    return prof / cost


def compute(d, latency_s, my_size):
    m = {}
    s7 = d.get("stats_7d") or {}
    s30 = d.get("stats_30d") or {}
    p1 = d.get("profits_1d") or {}
    pall = d.get("profits_all") or {}
    pnl = s7.get("pnl_stat") or {}
    common = s7.get("common") or {}

    m["buy"] = i(s7.get("buy", s7.get("buy_count")))
    m["sell"] = i(s7.get("sell", s7.get("sell_count")))
    m["trades"] = m["buy"] + m["sell"]
    m["per_day"] = m["trades"] / 7.0
    m["token_num"] = i(pnl.get("token_num"))
    m["winrate"] = f(pnl.get("winrate"))
    m["avg_hold_s"] = f(pnl.get("avg_holding_period"))
    m["realized_7d"] = f(s7.get("realized_profit"))
    m["cost_7d"] = f(s7.get("bought_cost", s7.get("total_cost")))
    m["avg_buy_usd"] = safe_div(m["cost_7d"], m["buy"])
    m["buckets"] = {
        "gt5": i(pnl.get("pnl_gt_5x_num")),
        "x2_5": i(pnl.get("pnl_2x_5x_num")),
        "x0_2": i(pnl.get("pnl_0x_2x_num")),
        "n50_0": i(pnl.get("pnl_nd5_0x_num")),
        "lt_n50": i(pnl.get("pnl_lt_nd5_num")),
    }
    m["lt50_share"] = safe_div(m["buckets"]["lt_n50"], max(1, m["token_num"]))
    m["winners"] = m["buckets"]["gt5"] + m["buckets"]["x2_5"] + m["buckets"]["x0_2"]

    # identity
    m["tags"] = common.get("tags") or ([common["tag"]] if common.get("tag") else [])
    m["tag_info"] = read_tags(m["tags"])
    m["tag_sev"] = {t["sev"] for t in m["tag_info"]}
    m["twitter"] = common.get("twitter_username") or ""
    m["twitter_name"] = common.get("twitter_name") or common.get("name") or ""
    m["blue"] = bool(common.get("is_blue_verified"))
    m["followers"] = i(common.get("followers_count"))
    m["created_tokens_n"] = i(common.get("created_token_count"))
    m["created_at"] = i(common.get("created_at"))
    m["age_days"] = (time.time() - m["created_at"]) / 86400 if m["created_at"] else None
    m["fund_from"] = common.get("fund_from") or ""
    m["fund_from_address"] = common.get("fund_from_address") or ""
    m["fund_amount"] = f(common.get("fund_amount"))
    m["follow_count"] = i(common.get("follow_count"))
    m["is_dev"] = m["created_tokens_n"] > 0 and m["created_tokens_n"] > 0.5 * max(1, m["token_num"])

    # form curve — the trap detector: great all-time, dead this week
    def stats_roi(row):
        """`realized_profit_pnl` is a ratio, not a percentage. A zero cost basis means
        the window has no closed trades — that is 'unknown', never 'zero return'."""
        if not row:
            return None
        cost = f(row.get("bought_cost", row.get("total_cost")))
        if cost <= 0:
            return None
        return f(row.get("realized_profit_pnl", row.get("pnl")))

    m["roi_1d"] = window_roi(p1, "realized_profit_cost", "realized_profit")
    m["roi_7d"] = stats_roi(s7)
    m["roi_30d"] = stats_roi(s30)
    m["roi_all"] = window_roi(pall, "total_realized_profit_cost", "total_realized_profit")
    m["realized_all"] = f(pall.get("total_realized_profit")) if pall else None
    m["unrealized"] = f(pall.get("unrealized_profit")) if pall else None
    m["realized_1d"] = f(p1.get("realized_profit")) if p1 else None

    r7, ra = m["roi_7d"], m["roi_all"]
    if r7 is None or ra is None:
        m["form"] = ("⚪", _("无法判断", "unknown"))
    elif ra <= 0 and r7 <= 0:
        m["form"] = ("⚫", _("长期亏", "never worked"))
    elif ra > 0.1 and r7 <= -0.1:
        m["form"] = ("💀", _("崩坏", "broken down"))
    elif r7 > max(0.1, ra):
        m["form"] = ("🔥", _("升温", "heating up"))
    elif abs(r7 - ra) <= 0.15:
        m["form"] = ("➡️", _("持平", "steady"))
    elif r7 < ra - 0.15:
        m["form"] = ("❄️", _("退潮", "cooling off"))
    else:
        m["form"] = ("➡️", _("持平", "steady"))

    # ── activity-derived behaviour ──
    acts = d.get("activity") or []
    trade_rows = [a for a in acts if ev_type(a) in ("buy", "sell")]
    m["sampled"] = len(trade_rows)
    ts = [f(a.get("timestamp")) for a in trade_rows if f(a.get("timestamp")) > 0]
    m["span_h"] = (max(ts) - min(ts)) / 3600 if len(ts) >= 2 else 0.0
    m["hit_limit"] = len(acts) >= 300

    mcaps, gas, buy_costs = [], [], []
    by_tok = {}
    for a in trade_rows:
        et = ev_type(a)
        addr = tok_addr(a)
        if addr:
            by_tok.setdefault(addr, []).append(a)
        if f(a.get("gas_usd")) > 0:
            gas.append(f(a.get("gas_usd")))
        if et == "buy":
            sup = f((a.get("token") or {}).get("total_supply"))
            px = f(a.get("price_usd"))
            if sup > 0 and px > 0:
                mcaps.append(px * sup)
            if f(a.get("cost_usd")) > 0:
                buy_costs.append(f(a.get("cost_usd")))
    m["avg_gas_usd"] = safe_div(sum(gas), len(gas))
    m["median_buy_usd"] = med(buy_costs)
    # gas as a share of trade size — the number that says whether the edge survives friction
    denom = m["median_buy_usd"] or m["avg_buy_usd"]
    m["gas_share"] = (m["avg_gas_usd"] / denom) if (gas and denom > 0) else None
    m["entry_p25"] = quantile(mcaps, 0.25)
    m["entry_p50"] = quantile(mcaps, 0.50)
    m["entry_p75"] = quantile(mcaps, 0.75)
    m["entry_n"] = len(mcaps)
    m["entry_sub100k"] = safe_div(sum(1 for x in mcaps if x < 100_000), len(mcaps))

    copy_windows, accum_windows, buys_per_tok, sells_per_tok, flip5 = [], [], [], [], 0
    round_trips = 0
    dump_shape = 0
    for addr, evs in by_tok.items():
        evs = sorted(evs, key=lambda e: f(e.get("timestamp")))
        buys = [e for e in evs if ev_type(e) == "buy"]
        sells = [e for e in evs if ev_type(e) == "sell"]
        buys_per_tok.append(len(buys))
        sells_per_tok.append(len(sells))
        if buys and sells:
            t_first_buy = f(buys[0].get("timestamp"))
            t_first_sell = f(sells[0].get("timestamp"))
            if t_first_sell > t_first_buy:
                copy_windows.append(t_first_sell - t_first_buy)
                round_trips += 1
                if t_first_sell - t_first_buy <= 5:
                    flip5 += 1
        if len(buys) >= 2:
            accum_windows.append(f(buys[-1].get("timestamp")) - f(buys[0].get("timestamp")))
        if sells:
            sold = sum(f(e.get("cost_usd")) for e in sells)
            biggest = max(f(e.get("cost_usd")) for e in sells)
            if sold > 0 and biggest / sold >= 0.8:
                dump_shape += 1
    m["copy_window_s"] = med(copy_windows)
    m["copy_window_n"] = len(copy_windows)
    m["accum_window_s"] = med(accum_windows)
    m["avg_buys_per_token"] = safe_div(sum(buys_per_tok), len(buys_per_tok))
    m["avg_sells_per_token"] = safe_div(sum(sells_per_tok), len(sells_per_tok))
    m["flip5_rate"] = safe_div(flip5, round_trips)
    m["dump_share"] = safe_div(dump_shape, max(1, sum(1 for v in sells_per_tok if v)))
    m["distinct_tokens_sampled"] = len(by_tok)

    # last 24h posture — what it is doing RIGHT NOW
    now = max(ts) if ts else time.time()
    b24 = s24 = 0.0
    recent_buys = {}
    for a in trade_rows:
        if now - f(a.get("timestamp")) > 86400:
            continue
        c = f(a.get("cost_usd"))
        if ev_type(a) == "buy":
            b24 += c
            sym = (a.get("token") or {}).get("symbol") or (tok_addr(a) or "?")[:6]
            recent_buys[sym] = recent_buys.get(sym, 0.0) + c
        else:
            s24 += c
    m["buy_usd_24h"], m["sell_usd_24h"] = b24, s24
    m["recent_buys"] = sorted(recent_buys.items(), key=lambda kv: -kv[1])[:5]
    if b24 + s24 <= 0:
        m["posture"] = ("😴", _("24h 静默", "quiet for 24h"))
    elif s24 > 2 * b24:
        m["posture"] = ("📤", _("在出货", "distributing"))
    elif b24 > 2 * s24:
        m["posture"] = ("🧊", _("在建仓", "accumulating"))
    else:
        m["posture"] = ("🔁", _("对冲/换仓", "rotating"))

    # ── holdings-derived: profit concentration + hold-to-zero ──
    h = d.get("holdings") or []
    m["holdings_n"] = len(h)
    m["pcr"] = None
    m["pcr_source"] = None
    m["pcr_trusted"] = False
    m["one_coin_note"] = None
    if h:
        profits = sorted((f(x.get("total_profit")) for x in h), reverse=True)  # confirmed name
        pos = [p for p in profits if p > 0]
        if pos:
            m["pcr"] = safe_div(pos[0], sum(pos))
            m["pcr_source"] = "holdings"
            # A concentration ratio over 2 winners is arithmetic, not evidence. Requiring
            # 3+ winners and 8+ positions is what stops this vetoing every small sample:
            # with one winner in the page, PCR is 100% by definition.
            m["pcr_trusted"] = len(pos) >= 3 and len(h) >= 8
        m["hold_to_zero"] = sum(
            1
            for x in h
            if f(h_get(x, "total_profit_pnl", "profit_change")) <= -0.9
            and i(h_get(x, "history_total_sells", "sell_tx_count")) == 0
        )
        m["open_book"] = [
            {
                "sym": (x.get("token") or {}).get("symbol") or "?",
                "usd": f(x.get("usd_value")),
                "chg": f(h_get(x, "total_profit_pnl", "profit_change")),
                "cost": f(h_get(x, "accu_cost", "cost", "history_bought_cost")),
                "sells": i(h_get(x, "history_total_sells", "sell_tx_count")),
            }
            for x in sorted(h, key=lambda x: -f(x.get("usd_value")))[:5]
            if f(x.get("usd_value")) > 0
        ]
        m["open_value"] = sum(f(x.get("usd_value")) for x in h)
    else:
        m["hold_to_zero"] = None
        m["open_book"] = []
        m["open_value"] = None

    # One-coin detector, independent of holdings. This is a *count* fact from the P&L
    # buckets — never a synthesised percentage. A wallet that is net positive on the
    # strength of at most one >200% token, with a losing majority, was carried by that token.
    big = m["buckets"]["gt5"] + m["buckets"]["x2_5"]
    losers = m["buckets"]["n50_0"] + m["buckets"]["lt_n50"]
    if (
        m["realized_7d"] > 0
        and big <= 1
        and m["token_num"] >= 8
        and losers > 0.5 * m["token_num"]
    ):
        m["one_coin_note"] = _(
            f"{m['token_num']} 个币里只有 {big} 个翻过 2 倍，{losers} 个亏损，却整体盈利 "
            f"{usd(m['realized_7d'])} —— 利润几乎只来自那一个币",
            f"of {m['token_num']} tokens only {big} cleared 2x while {losers} lost money, yet the wallet "
            f"is up {usd(m['realized_7d'])} — the profit came from that one token",
        )

    # ── dev record ──
    ct = d.get("created_tokens") or {}
    if ct:
        m["dev_open"] = i(ct.get("open_count"))
        m["dev_inner"] = i(ct.get("inner_count"))
        m["dev_total"] = m["dev_open"] + m["dev_inner"]
        m["dev_open_ratio"] = (
            f(ct.get("open_ratio")) if ct.get("open_ratio") is not None
            else safe_div(m["dev_open"], max(1, m["dev_total"]))
        )
        ath = ct.get("creator_ath_info") or {}
        m["dev_ath_mc"] = f(ath.get("ath_mc"))
    else:
        m["dev_total"] = None

    # ── friction: the numbers that decide whether the edge survives being copied ──
    # Per-trade net is the yardstick everything else is measured against. A wallet netting
    # $26 a trade while paying $4 of gas has already given a third of its edge away, and
    # your slippage comes out of what is left.
    m["net_per_sell"] = safe_div(m["realized_7d"], m["sell"])
    if m["avg_gas_usd"] > 0 and m["trades"] > 0 and m["realized_7d"] > 0:
        m["gas_total_est"] = m["avg_gas_usd"] * m["trades"]
        m["gas_drag"] = m["gas_total_est"] / m["realized_7d"]
    else:
        m["gas_total_est"] = None
        m["gas_drag"] = None

    # Reconcile the average against the median. `avg_holding_period` counts every position
    # including bags never sold, so a scalper can report a 4-day "average hold". Reporting
    # both without saying which is which is exactly the reasoning burden to remove.
    m["hold_conflict"] = None
    if m["avg_hold_s"] > 0 and m["copy_window_n"] >= 3 and m["copy_window_s"] > 0:
        if m["avg_hold_s"] > 8 * m["copy_window_s"]:
            m["hold_conflict"] = _(
                f"接口给的平均持仓 {dur(m['avg_hold_s'])}，但活跃样本里首买→首卖的中位只有 "
                f"{dur(m['copy_window_s'])} —— 均值被少数一直没卖的仓位拖高。看中位，别看均值",
                f"the API's average hold is {dur(m['avg_hold_s'])}, but the median first-buy→first-sell in "
                f"the live sample is {dur(m['copy_window_s'])} — the mean is dragged up by bags it never "
                f"sold. Read the median, not the mean",
            )

    # ── honeypots in the live book ──
    # `token.is_honeypot` ships inline on every holdings row, so this costs nothing and is
    # available whenever holdings is. `security_checked` records how many rows actually
    # carried the flag, so a missing flag is never read as "clean".
    hp_names, flagged = [], 0
    for h_row in (d.get("holdings") or []):
        tk = h_row.get("token") or {}
        if tk.get("is_honeypot") is None:
            continue
        flagged += 1
        if _b(tk.get("is_honeypot")):
            hp_names.append({"sym": tk.get("symbol") or (tok_addr(h_row) or "?")[:6],
                             "usd": f(h_row.get("usd_value"))})
    m["honeypots"] = hp_names
    m["honeypot_usd"] = sum(x["usd"] for x in hp_names)
    m["security_checked"] = flagged

    # Where it hunts — launchpad mix across the live book, also inline on token.
    lp = {}
    for h_row in (d.get("holdings") or []):
        name = ((h_row.get("token") or {}).get("launchpad_platform")
                or (h_row.get("token") or {}).get("launchpad"))
        if name:
            lp[str(name)] = lp.get(str(name), 0) + 1
    m["launchpads"] = sorted(lp.items(), key=lambda kv: -kv[1])[:3]

    # size guidance
    m["my_size"] = my_size
    m["size_cap"] = m["avg_buy_usd"] * 0.5 if m["avg_buy_usd"] > 0 else None
    m["latency_s"] = latency_s
    return m


# ─────────────────────────── the four gates ───────────────────────────


def gates(m):
    """Each gate: (pass?, one-line reason with the number that decided it)."""
    g = {}

    # No trades at all: nothing is assessable. Every gate is ⚪, not ❌ — "unevaluated"
    # and "failed" must never render the same, or a fresh wallet reads as a bad wallet.
    if m["trades"] == 0:
        blank = _("7 天内没有买卖记录，无从评估", "no buys or sells in 7 days — nothing to evaluate")
        return {k: (None, blank) for k in ("G1", "G2", "G3", "G4")}

    # G1 AUTHENTICITY — a wash-trading marker outranks every other test here. If the
    # volume may be self-dealt, the win rate, the ROI and the bucket distribution are all
    # measuring the wallet trading against itself, and no amount of good-looking
    # distribution rescues that.
    wash = [t for t in m["tag_info"] if t["sev"] == "veto_g1"]
    if wash:
        names = "、".join(t["name"] for t in wash) if ZH else ", ".join(t["name"] for t in wash)
        g["G1"] = (
            False,
            _(
                f"GMGN 标记「{names}」—— {wash[0]['meaning']}。"
                f"这 7 天 {usd(m['realized_7d'])} 的已实现盈亏不可采信",
                f"GMGN flags this wallet as {names} — {wash[0]['meaning']}. The "
                f"{usd(m['realized_7d'])} realized P&L in this window cannot be taken at face value",
            ),
        )
    elif m["is_dev"]:
        g["G1"] = (
            False,
            _(
                f"发币方钱包：自己发了 {m['created_tokens_n']} 个币 / 交易过 {m['token_num']} 个 —— "
                "胜率和入场时机是自己写的，不是市场读出来的",
                f"launcher wallet: created {m['created_tokens_n']} vs traded {m['token_num']} — "
                "its win rate and entry timing are self-authored, not a market read",
            ),
        )
    elif m["token_num"] < 5:
        g["G1"] = (
            False,
            _(
                f"样本只有 {m['token_num']} 个币，任何比率都不成立",
                f"only {m['token_num']} tokens — no ratio computed on this is meaningful",
            ),
        )
    elif m["one_coin_note"]:
        g["G1"] = (False, m["one_coin_note"])
    elif m["pcr_trusted"] and m["pcr"] >= 0.75:
        g["G1"] = (
            False,
            _(
                f"利润集中度 {pct(m['pcr'])}（{m['holdings_n']} 个仓位口径）—— 一个币扛起了整份战绩，复制不了",
                f"profit concentration {pct(m['pcr'])} (across {m['holdings_n']} positions) — one coin carried the record",
            ),
        )
    else:
        if m["pcr_trusted"]:
            pcr_txt = _(f"利润集中度 {pct(m['pcr'])}", f"profit concentration {pct(m['pcr'])}")
        elif m["pcr"] is not None:
            pcr_txt = _(
                f"利润集中度 {pct(m['pcr'])}（仅 {m['holdings_n']} 个仓位，样本太薄，未作为判据）",
                f"profit concentration {pct(m['pcr'])} (only {m['holdings_n']} positions — too thin to rely on)",
            )
        else:
            pcr_txt = _("利润集中度未测（holdings 不可用）", "profit concentration not measured (holdings unavailable)")
        g["G1"] = (
            True,
            _(
                f"{m['token_num']} 个币、{m['winners']} 个盈利，{pcr_txt}",
                f"{m['token_num']} tokens, {m['winners']} profitable, {pcr_txt}",
            ),
        )

    # G2 CURRENCY
    emoji, label = m["form"]
    r7 = m["roi_7d"]
    ra = m["roi_all"]
    r7t = pct(r7) if r7 is not None else "n/a"
    rat = pct(ra) if ra is not None else "n/a"
    if label in (_("崩坏", "broken down"), _("长期亏", "never worked")):
        g["G2"] = (
            False,
            _(
                f"{emoji} {label}：7d {r7t} vs 全期 {rat}",
                f"{emoji} {label}: 7d {r7t} vs all-time {rat}",
            ),
        )
    elif r7 is not None and r7 <= 0 and m["roi_30d"] is not None and m["roi_30d"] <= 0:
        g["G2"] = (
            False,
            _(
                f"近 7d 和 30d 都是负的（{r7t} / {pct(m['roi_30d'] or 0)}）",
                f"both 7d and 30d are negative ({r7t} / {pct(m['roi_30d'] or 0)})",
            ),
        )
    else:
        g["G2"] = (
            True,
            _(
                f"{emoji} {label}：7d {r7t} vs 全期 {rat}",
                f"{emoji} {label}: 7d {r7t} vs all-time {rat}",
            ),
        )

    # G3 REACHABILITY
    cw = m["copy_window_s"]
    lat = m["latency_s"]
    reasons_fail, reasons_ok = [], []
    if m["copy_window_n"] >= 3:
        # 3x is the margin, not 1x: landing at the very edge of the window means every
        # slow block, RPC hiccup, or confirmation delay puts you on the wrong side of its exit.
        if cw < lat * 3:
            reasons_fail.append(
                _(
                    f"可跟窗口中位 {dur(cw)}，你的延迟 {dur(lat)} —— 余量不足 3 倍，"
                    "你买进去的时候它大概已经在卖了",
                    f"median copy window {dur(cw)} against your {dur(lat)} latency — under 3x margin, "
                    "it is likely already selling when you land",
                )
            )
        else:
            reasons_ok.append(
                _(f"可跟窗口 {dur(cw)}（延迟预算 {dur(lat)}）",
                  f"copy window {dur(cw)} (your latency budget {dur(lat)})")
            )
    if m["entry_n"] >= 5:
        if m["entry_p50"] > 0 and m["entry_p50"] < 30_000:
            reasons_fail.append(
                _(
                    f"入场市值中位 {mc(m['entry_p50'])} —— 这是狙击/内盘位，你进场就是它的 5–10 倍成本",
                    f"median entry mcap {mc(m['entry_p50'])} — sniper/pre-graduation territory; you enter at 5–10x its cost",
                )
            )
        else:
            reasons_ok.append(
                _(f"入场市值 p25/p50/p75 = {mc(m['entry_p25'])}/{mc(m['entry_p50'])}/{mc(m['entry_p75'])}",
                  f"entry mcap p25/p50/p75 = {mc(m['entry_p25'])}/{mc(m['entry_p50'])}/{mc(m['entry_p75'])}")
            )
    for t in m["tag_info"]:
        if t["sev"] == "veto_g3":
            reasons_fail.append(_(f"GMGN 标记「{t['name']}」—— {t['meaning']}",
                                  f"GMGN flags it as {t['name']} — {t['meaning']}"))
    if m["followers"] >= 10_000 and (m["entry_p50"] == 0 or m["entry_p50"] < 1_000_000):
        reasons_fail.append(
            _(
                f"公开身份 {m['followers']:,} 粉丝且主打小市值 —— 跟单盘在你之前就已经推过价",
                f"a public identity with {m['followers']:,} followers trading small caps — copy flow "
                f"has already moved the price before your order",
            )
        )
    # Gas that eats a large share of the per-trade net leaves nothing for your slippage.
    if m["gas_drag"] is not None and m["gas_drag"] >= 0.25:
        reasons_fail.append(
            _(
                f"估算 gas 吃掉利润的 {pct(m['gas_drag'])}（{m['trades']:,} 笔 × 均 "
                f"{usd(m['avg_gas_usd'])} ≈ {usd(m['gas_total_est'])} vs 已实现 {usd(m['realized_7d'])}）"
                f"，单笔净赚只有 {usd(m['net_per_sell'])} —— 你的滑点没有空间",
                f"gas is an estimated {pct(m['gas_drag'])} of the profit ({m['trades']:,} trades × "
                f"{usd(m['avg_gas_usd'])} ≈ {usd(m['gas_total_est'])} vs {usd(m['realized_7d'])} realized), "
                f"leaving {usd(m['net_per_sell'])} net per trade — no room for your slippage",
            )
        )
    if m["avg_buy_usd"] > 0 and m["avg_buy_usd"] < 50:
        reasons_fail.append(
            _(
                f"平均单笔买入 {usd(m['avg_buy_usd'])} —— 边际薄到手续费和滑点就吃掉了",
                f"average buy {usd(m['avg_buy_usd'])} — thin enough that fees and slippage eat the edge",
            )
        )
    if m["per_day"] > 100:
        reasons_fail.append(
            _(
                f"{m['per_day']:,.0f} 笔/日 —— 机器节奏，人手跟不动",
                f"{m['per_day']:,.0f} trades/day — bot cadence, no hand can keep pace",
            )
        )
    if m["copy_window_n"] < 3 and m["entry_n"] < 5:
        g["G3"] = (
            None,
            _("activity 样本不足，可及性未评估", "activity sample too thin — reachability not evaluated"),
        )
    elif reasons_fail:
        g["G3"] = (False, reasons_fail)
    else:
        g["G3"] = (True, reasons_ok or [_("未发现可及性障碍", "no reachability obstacle found")])

    # G4 SURVIVABILITY
    if m["token_num"] < 5:
        g["G4"] = (None, _("样本不足，生存性未评估", "sample too thin — survivability not evaluated"))
    elif len(m["honeypots"]) >= 2:
        syms = "、".join(x["sym"] for x in m["honeypots"]) if ZH else ", ".join(x["sym"] for x in m["honeypots"])
        g["G4"] = (
            False,
            _(
                f"当前持仓里 {len(m['honeypots'])} 个是蜜罐（{syms}，合计 {usd(m['honeypot_usd'])} "
                f"卖不出来）—— 它自己的风控就没挡住，你照抄会踩同样的坑",
                f"{len(m['honeypots'])} live positions are honeypots ({syms}, {usd(m['honeypot_usd'])} "
                f"that cannot be sold) — its own screening did not catch them, and copying it walks into the same ones",
            ),
        )
    elif m["lt50_share"] >= 0.35:
        g["G4"] = (
            False,
            _(
                f"{pct(m['lt50_share'])} 的币亏超 50%（{m['buckets']['lt_n50']}/{m['token_num']}）—— 不砍仓",
                f"{pct(m['lt50_share'])} of its tokens are down >50% ({m['buckets']['lt_n50']}/{m['token_num']}) — it does not cut",
            ),
        )
    elif m["hold_to_zero"] is not None and m["hold_to_zero"] >= 3:
        g["G4"] = (
            False,
            _(
                f"{m['hold_to_zero']} 个仓位亏 90%+ 且一次没卖 —— 抱到归零是常态",
                f"{m['hold_to_zero']} positions down 90%+ with zero sells — riding to zero is the habit",
            ),
        )
    else:
        reasons = [
            _(f"重亏占比 {pct(m['lt50_share'])}（{m['buckets']['lt_n50']}/{m['token_num']} 个币亏超 50%）",
              f"heavy-loss share {pct(m['lt50_share'])} ({m['buckets']['lt_n50']}/{m['token_num']} down >50%)")
        ]
        if m["hold_to_zero"] is not None:
            reasons.append(_(f"抱到归零 {m['hold_to_zero']} 个（亏 90%+ 且零卖出）",
                             f"{m['hold_to_zero']} ridden to zero (down 90%+ with zero sells)"))
        if m["security_checked"]:
            reasons.append(_(f"已检查 {m['security_checked']} 个持仓的蜜罐标记，无命中",
                             f"honeypot flag checked on {m['security_checked']} positions, none hit"))
        else:
            reasons.append(_("⚪ 蜜罐未检查（holdings 不可用）—— 本项通过仅基于砍仓行为，不含蜜罐",
                             "⚪ honeypot NOT checked (holdings unavailable) — this pass covers "
                             "loss-cutting only, not honeypots"))
        g["G4"] = (True, reasons)

    # A launcher's entry timing and loss-cutting are measurements of its own token's
    # price, which it controls. Reporting them as ✅ would be reporting self-dealing
    # as skill — so they are marked unevaluated, not passed.
    if m["is_dev"]:
        na = _(
            "发币方钱包，该项衡量的是它对自己代币的操作，不成立",
            "launcher wallet — this measures its handling of its own token, so it does not apply",
        )
        g["G3"] = (None, na)
        g["G4"] = (None, na)
    return g


def verdict(m, g):
    """Returns (emoji, headline, what-to-do). The headline names the cause; the third
    element is the ACTION, never a repeat of the gate reason shown further down."""
    p = {k: v[0] for k, v in g.items()}
    if m["trades"] == 0:
        return ("⚪",
                _("数据不足 · 不下判断", "NOT ENOUGH DATA · no verdict"),
                _("先确认这是钱包地址而不是代币合约——下面有三步检查。",
                  "First confirm this is a wallet and not a token contract — three checks below."))
    if p["G1"] is False:
        if any(t["sev"] == "veto_g1" for t in m["tag_info"]):
            return ("🔴",
                    _("别碰 · 刷量标记，战绩不可采信", "DO NOT COPY · wash-trading flag voids the record"),
                    _("把它的盈亏数字当作未知。想看它买什么可以，但别把这份战绩当依据。",
                      "Treat its P&L as unknown. Watch what it buys if you like, but do not "
                      "use this record as evidence."))
        if m["is_dev"]:
            return ("🔴",
                    _("别碰 · 发币方自导自演", "DO NOT COPY · a launcher marking its own homework"),
                    _("别评估它的交易能力，去查它历史发币的毕业率和安全记录（gmgn-wallet-score 的 Dev 角度）。",
                      "Do not score its trading — check its launch survival and security record "
                      "(gmgn-wallet-score, Dev angle)."))
        if m["one_coin_note"]:
            return ("🔴",
                    _("别碰 · 战绩由一个币扛起", "DO NOT COPY · one token carried the whole record"),
                    _("别按这份战绩下注。等它在更多币上重复出来，再重新评。",
                      "Do not size off this record. Wait until it repeats across more tokens."))
        return ("🔴",
                _("先观察 · 样本太少，不足以判断", "WATCH FIRST · too few tokens to judge"),
                _(f"只有 {m['token_num']} 个币，任何比率都不成立。先加观察名单，等交易满 5 个币以上再评。",
                  f"Only {m['token_num']} tokens — no ratio here is meaningful. Watch it until it has "
                  f"traded 5 or more."))
    if p["G2"] is False:
        return ("🔴",
                _("别跟 · 手感已经没了", "DO NOT COPY · the edge has stopped working"),
                _("现在别跟。7 天后再跑一次这份分析，看是回暖还是继续退。",
                  "Not now. Re-run this in 7 days to see whether form recovers or keeps sliding."))
    if p["G3"] is False:
        return ("🟡",
                _("学它，别抄它单", "LEARN FROM IT, DO NOT COPY THE ENTRIES"),
                _("战绩真、手感在，但你吃不到它的价位。当信号源用：看它买什么、在什么市值买，"
                  "自己二次筛选后按自己的节奏进。",
                  "Real record, live edge, unreachable fills. Use it as a signal source: note what it "
                  "buys and at what market cap, then enter on your own terms."))
    if p["G4"] is False:
        return ("🟡",
                _("只跟进，不跟出", "COPY ENTRIES, SET YOUR OWN EXITS"),
                _("它会选币但不砍仓。可以跟它进场，止损必须用你自己的。",
                  "It picks well but does not cut. Take its entries; keep your own stop."))
    if p["G3"] is None or p["G4"] is None:
        return ("🟡",
                _("先观察 · 关键项没测到", "WATCH FIRST · key gates unmeasured"),
                _("四道闸门里有一道数据不足——先补齐（通常是配置 GMGN_PRIVATE_KEY）再决定。",
                  "One of the four gates lacked data — fill that in (usually by configuring "
                  "GMGN_PRIVATE_KEY) before deciding."))
    size = usd(m["size_cap"]) if m["size_cap"] else _("你自己的常规仓位", "your normal size")
    win = dur(m["copy_window_s"]) if m["copy_window_s"] > 0 else None
    return ("🟢",
            _("可以小仓跟 · 四道闸门全过", "COPYABLE AT SMALL SIZE · all four gates pass"),
            _(f"起步 ≤ {size}" + (f"，下单要落在它买入后 {win}以内。" if win else "。"),
              f"Start at ≤ {size}" + (f", landing within {win} of its buy." if win else ".")))


# ─────────────────────────── report ───────────────────────────

GATE_NAMES = {
    "G1": ("真实性", "AUTHENTICITY"),
    "G2": ("时效性", "CURRENCY"),
    "G3": ("可及性", "REACHABILITY"),
    "G4": ("生存性", "SURVIVABILITY"),
}

GATE_GLOSS = {
    "G1": ("数据可信吗", "is the data trustworthy"),
    "G2": ("现在还在赚吗", "is it still earning now"),
    "G3": ("你吃得到吗", "can you get filled"),
    "G4": ("它会砍仓吗", "does it cut losses"),
}


def mark(v):
    return {True: "✅", False: "❌", None: "⚪"}[v]


def archetype(m):
    """Say what kind of counterparty this is, before any number gets interpreted."""
    tags = []
    if m["is_dev"]:
        tags.append(_("🏭 发币方（自导自演）", "🏭 launcher (marks its own homework)"))
    if m["per_day"] > 50:
        tags.append(_(f"🤖 机器级 {m['per_day']:,.0f} 笔/日", f"🤖 bot-tier {m['per_day']:,.0f} trades/day"))
    if m["entry_n"] >= 5 and 0 < m["entry_p50"] < 100_000:
        tags.append(_(f"🎯 狙击手 中位入场 {mc(m['entry_p50'])}", f"🎯 sniper, median entry {mc(m['entry_p50'])}"))
    if m["avg_buy_usd"] >= 10_000:
        tags.append(_(f"🐋 巨鲸 单笔均 {usd(m['avg_buy_usd'])}", f"🐋 whale, {usd(m['avg_buy_usd'])} per buy"))
    if m["age_days"] is not None and m["age_days"] < 30:
        tags.append(_(f"🆕 新号 {m['age_days']:.0f} 天", f"🆕 new wallet, {m['age_days']:.0f} days old"))
    if m["flip5_rate"] >= 0.3:
        tags.append(_(f"⚡ 秒抛 {pct(m['flip5_rate'])} 的回合 5 秒内出", f"⚡ 5-second flipper on {pct(m['flip5_rate'])} of round trips"))
    if m["avg_buys_per_token"] >= 3:
        tags.append(_(f"🧱 分批建仓 均 {m['avg_buys_per_token']:.1f} 笔/币", f"🧱 scales in, {m['avg_buys_per_token']:.1f} buys/token"))
    if m["dump_share"] >= 0.7 and m["sampled"] >= 20:
        tags.append(_(f"💣 一把清 {pct(m['dump_share'])} 的仓位单笔出完", f"💣 dumps in one go on {pct(m['dump_share'])} of exits"))
    return tags or [_("普通交易钱包，无特征标记", "ordinary trading wallet, no distinguishing marks")]


def roi_label(v):
    if v is None:
        return _("未知", "unknown")
    if v > 0.5:
        return _("强盈", "strongly profitable")
    if v > 0.1:
        return _("净盈", "net positive")
    if abs(v) <= 0.1:
        return _("打平", "flat")
    if v > -0.3:
        return _("净亏", "net negative")
    return _("重伤", "badly down")


def cadence_label(per_day):
    if per_day > 50:
        return _("机器级，跟不动", "bot-tier, unfollowable")
    if per_day > 10:
        return _("高频，需脚本", "high freq, needs tooling")
    if per_day >= 1:
        return _("常规，可手动", "normal, hand-tradeable")
    return _("低频，样本慢", "low freq, slow evidence")


def entry_label(p50):
    if p50 <= 0:
        return _("未测", "not measured")
    if p50 < 30_000:
        return _("内盘位，进场即高价", "pre-graduation, you pay up")
    if p50 < 100_000:
        return _("狙击位，拿不到同价", "sniper range, no match")
    if p50 < 300_000:
        return _("小市值，滑点大", "small cap, heavy slippage")
    if p50 < 3_000_000:
        return _("中市值，可跟", "mid cap, copyable")
    return _("大市值，容量足", "large cap, deep")


def friction_label(m):
    if m["gas_drag"] is None:
        return _("gas 数据不足，未评估", "not enough gas data to evaluate")
    if m["gas_drag"] >= 0.25:
        return _("摩擦吃掉大头", "friction eats the bulk")
    if m["gas_drag"] >= 0.10:
        return _("摩擦不小", "meaningful friction")
    return _("摩擦可控", "friction manageable")


def speed_read(m, g, why):
    """Three lines, each a finished thought. Nothing here requires the reader to compute."""
    rows = []
    marks = [t for t in archetype(m) if not t.startswith("普通") and not t.startswith("ordinary")]
    rows.append((_("定性", "what it is"),
                 " · ".join(marks[:2]) if marks else _("普通交易钱包，无特征标记",
                                                       "ordinary trading wallet, no distinguishing marks")))
    key = []
    if m["per_day"] > 10:
        key.append(_(f"{m['per_day']:,.0f} 笔/日", f"{m['per_day']:,.0f} trades/day"))
    if m["gas_drag"] is not None and m["gas_drag"] >= 0.10:
        key.append(_(f"单笔净赚 {usd(m['net_per_sell'])} vs gas {usd(m['avg_gas_usd'])}",
                     f"{usd(m['net_per_sell'])} net vs {usd(m['avg_gas_usd'])} gas"))
    if m["entry_p50"] > 0:
        key.append(_(f"入场中位 {mc(m['entry_p50'])}", f"median entry {mc(m['entry_p50'])}"))
    if m["roi_7d"] is not None:
        key.append(_(f"7d {pct(m['roi_7d'])}", f"7d {pct(m['roi_7d'])}"))
    if m["copy_window_n"] >= 3:
        key.append(_(f"可跟窗口 {dur(m['copy_window_s'])}", f"copy window {dur(m['copy_window_s'])}"))
    rows.append((_("关键数字", "key numbers"), " · ".join(key[:4]) or _("样本不足", "sample too thin")))

    flags = [t for t in m["tag_info"] if t["sev"] in ("veto_g1", "veto_g3")] or \
            [t for t in m["tag_info"] if t["sev"] == "warn"]
    if m["honeypots"]:
        rows.append((_("最大风险", "top risk"),
                     _(f"持仓里 {len(m['honeypots'])} 个蜜罐，{usd(m['honeypot_usd'])} 卖不出来"
                       " —— 它自己也会踩雷",
                       f"{len(m['honeypots'])} honeypots in its live book, {usd(m['honeypot_usd'])} "
                       "unsellable — its own screening fails too")))
    elif flags:
        rows.append((_("最大风险", "top risk"), f"{flags[0]['emoji']} {flags[0]['name']} · {flags[0]['meaning']}"))
    elif m["lt50_share"] >= 0.35:
        rows.append((_("最大风险", "top risk"),
                     _(f"{pct(m['lt50_share'])} 的币亏超 50% —— 它不砍仓",
                       f"{pct(m['lt50_share'])} of tokens down >50% — it does not cut")))
    elif not m["security_checked"]:
        rows.append((_("最大风险", "top risk"),
                     _("无高危旗标，但蜜罐与当前持仓未检查（holdings 不可用）",
                       "no high-severity flags — but honeypots and the live book were not checked")))
    else:
        rows.append((_("最大风险", "top risk"), _("无高危旗标", "no high-severity flags")))

    return rows


def report(wallet, chain, m, g, gaps):
    out = []
    w = wallet if len(wallet) <= 14 else f"{wallet[:6]}…{wallet[-4:]}"
    emoji, headline, why = verdict(m, g)
    BAR = "━" * 66

    # ── 判决：第一屏只有这一件事 ──
    out.append(BAR)
    out.append(f"{emoji} {headline}")
    out.append(BAR)
    put(out, _("怎么办  ", "DO THIS  "), why)
    out.append("")
    out.append(
        _(f"{w} · {chain} · 数据区间 7d（全期数据来自 profits --period all）",
          f"{w} · {chain} · window 7d (all-time from profits --period all)")
    )
    out.append("")

    if m["trades"] == 0:
        out.append(_("下一步", "NEXT"))
        for step in (
            _("确认这是钱包地址而不是代币合约（代币合约也能查通，但每项都返回 0，看起来像答案，其实不是）。",
              "Confirm this is a wallet, not a token contract — a contract queries fine and returns "
              "zeros everywhere, which looks like an answer and is not one."),
            _("确认链选对了：base58 → sol，0x → bsc/base/eth。",
              "Confirm the chain: base58 → sol, 0x → bsc/base/eth."),
            _("确认是钱包后，用 gmgn-portfolio holdings 看它是否只收过转账/空投。",
              "If it is a wallet, use gmgn-portfolio holdings to see whether it only ever received "
              "transfers or airdrops."),
        ):
            put(out, "  • ", step)
        if gaps:
            out.append("")
            out.append(_("数据缺口：", "DATA GAPS:"))
            for gp in gaps:
                out.append(f"  ⚪ {gp}")
        return "\n".join(out)

    # ── 速读：三行读完，不需要往下翻 ──
    out.append(_("⚡ 速读", "⚡ SPEED READ"))
    for lab, val in speed_read(m, g, why):
        put(out, f"  {wpad(lab, 10)} ", val)
    out.append("")

    # ── 四道闸门 ──
    strip = "  ".join(f"{mark(g[k][0])}{k}" for k in ("G1", "G2", "G3", "G4"))
    out.append(_(f"🚦 四道闸门    {strip}", f"🚦 THE FOUR GATES    {strip}"))
    for k in ("G1", "G2", "G3", "G4"):
        zh, en = GATE_NAMES[k]
        gz, ge = GATE_GLOSS[k]
        gloss = f"（{_(gz, ge)}）" if ZH else f" ({_(gz, ge)})"
        out.append(f"  {mark(g[k][0])} {k} {_(zh, en)}{gloss}")
        detail = g[k][1]
        for item in (detail if isinstance(detail, list) else [detail]):
            put(out, "     • ", item, hang=7)
    out.append("")

    # ── 风险旗标：二元事实，不用读段落 ──
    risk = []
    for t in m["tag_info"]:
        if t["sev"] in ("veto_g1", "veto_g3", "warn"):
            risk.append(f"{t['emoji']} {t['name']} · {t['meaning']}")
    if m["honeypots"]:
        syms = "、".join(x["sym"] for x in m["honeypots"]) if ZH else ", ".join(x["sym"] for x in m["honeypots"])
        risk.append(_(f"🍯 蜜罐持仓 {len(m['honeypots'])} 个（{syms}）· {usd(m['honeypot_usd'])} 卖不出来",
                      f"🍯 {len(m['honeypots'])} honeypot positions ({syms}) · {usd(m['honeypot_usd'])} unsellable"))
    good = [f"{t['emoji']} {t['name']} · {t['meaning']}" for t in m["tag_info"] if t["sev"] == "good"]
    # A clean screen is reassurance, not a risk — it must not inflate the risk count.
    if not m["honeypots"] and m["security_checked"]:
        good.append(_(f"✅ 已检查 {m['security_checked']} 个持仓的蜜罐标记，无命中",
                      f"✅ honeypot flag checked on {m['security_checked']} positions, none hit"))
    if risk:
        out.append(_(f"🚩 风险旗标（{len(risk)}）", f"🚩 RISK FLAGS ({len(risk)})"))
        for r in risk:
            put(out, "  ", r)
    else:
        out.append(_("✅ 无风险旗标", "✅ NO RISK FLAGS"))
    for gd in good:
        put(out, "  ", gd)
    if risk or good:
        out.append("")

    # ── 身份 ──
    idl = []
    if m["twitter_name"] or m["twitter"]:
        who = m["twitter_name"] or ""
        if m["twitter"]:
            who += f" @{m['twitter']}"
        bits = [who.strip()]
        if m["blue"]:
            bits.append(_("蓝V", "blue-verified"))
        if m["followers"]:
            bits.append(_(f"{m['followers']:,} 粉丝", f"{m['followers']:,} followers"))
        idl.append(" · ".join(bits))
    neutral = [f"{t['emoji']} {t['name']}" for t in m["tag_info"] if t["sev"] == "neutral"]
    prov = list(neutral)
    if m["age_days"] is not None:
        prov.append(_(f"钱包 {m['age_days']:.0f} 天", f"{m['age_days']:.0f}-day-old wallet"))
    if m["fund_from"] or m["fund_from_address"]:
        src = m["fund_from"] or f"{m['fund_from_address'][:6]}…"
        prov.append(_(f"资金来自 {src}", f"funded from {src}")
                    + (f" {usd(m['fund_amount'])}" if m["fund_amount"] else ""))
    if m["launchpads"]:
        prov.append(_("主要打 " + "、".join(f"{k}×{v}" for k, v in m["launchpads"]),
                      "hunts on " + ", ".join(f"{k}×{v}" for k, v in m["launchpads"])))
    if m["dev_total"]:
        prov.append(_(f"发过 {m['dev_total']} 个币（毕业 {m['dev_open']} · 毕业率 {pct(m['dev_open_ratio'])}）",
                      f"launched {m['dev_total']} tokens ({m['dev_open']} graduated · {pct(m['dev_open_ratio'])})"))
    elif m["created_tokens_n"]:
        prov.append(_(f"发过 {m['created_tokens_n']} 个币", f"launched {m['created_tokens_n']} tokens"))
    if prov:
        idl.append(" · ".join(prov))
    for t in archetype(m):
        if not (t.startswith("普通") or t.startswith("ordinary")):
            idl.append(t)
    if idl:
        out.append(_("👤 它是谁", "👤 WHO IT IS"))
        for line in idl:
            put(out, "  ", line)
        out.append("")

    # ── 数字面板：每行自带结论 ──
    out.append(_("📊 数字面板（每行右侧是结论，不用自己算）", "📊 NUMBERS (the conclusion is on the right)"))
    rows = []
    rows.append((_("盈亏", "P&L"),
                 _(f"{usd(m['realized_7d'])} / 成本 {usd(m['cost_7d'])} = "
                   f"{pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a'}",
                   f"{usd(m['realized_7d'])} on {usd(m['cost_7d'])} cost = "
                   f"{pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a'}"),
                 roi_label(m["roi_7d"])))
    rows.append((_("手感", "form"),
                 _(f"1d {pct(m['roi_1d']) if m['roi_1d'] is not None else 'n/a'} · "
                   f"7d {pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a'} · "
                   f"30d {pct(m['roi_30d']) if m['roi_30d'] is not None else 'n/a'} · "
                   f"全期 {pct(m['roi_all']) if m['roi_all'] is not None else 'n/a'}",
                   f"1d {pct(m['roi_1d']) if m['roi_1d'] is not None else 'n/a'} · "
                   f"7d {pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a'} · "
                   f"30d {pct(m['roi_30d']) if m['roi_30d'] is not None else 'n/a'} · "
                   f"all {pct(m['roi_all']) if m['roi_all'] is not None else 'n/a'}"),
                 f"{m['form'][0]} {m['form'][1]}"))
    rows.append((_("节奏", "cadence"),
                 _(f"{m['trades']:,} 笔（{m['buy']:,} 买 / {m['sell']:,} 卖）= {m['per_day']:,.0f} 笔/日",
                   f"{m['trades']:,} trades ({m['buy']:,} buy / {m['sell']:,} sell) = {m['per_day']:,.0f}/day"),
                 cadence_label(m["per_day"])))
    fr = _(f"单笔净赚 {usd(m['net_per_sell'])} · gas 均 {usd(m['avg_gas_usd'])}",
           f"{usd(m['net_per_sell'])} net per exit · {usd(m['avg_gas_usd'])} avg gas")
    if m["gas_drag"] is not None:
        fr += _(f" ≈ 吃掉利润 {pct(m['gas_drag'])}", f" ≈ {pct(m['gas_drag'])} of profit")
    rows.append((_("摩擦", "friction"), fr, friction_label(m)))
    hold = _(f"均 {dur(m['avg_hold_s'])} · 可跟窗口中位 {dur(m['copy_window_s'])}（{m['copy_window_n']} 个回合）",
             f"mean {dur(m['avg_hold_s'])} · median copy window {dur(m['copy_window_s'])} ({m['copy_window_n']} round trips)")
    rows.append((_("持仓", "holding"), hold,
                 _("⚠️ 看中位", "⚠️ read the median")
                 if m["hold_conflict"] else _("均值可用", "mean is usable")))
    rows.append((_("入场", "entry"),
                 _(f"p25/p50/p75 {mc(m['entry_p25'])}/{mc(m['entry_p50'])}/{mc(m['entry_p75'])}"
                   f"（{m['entry_n']} 笔可测）",
                   f"p25/p50/p75 {mc(m['entry_p25'])}/{mc(m['entry_p50'])}/{mc(m['entry_p75'])}"
                   f" ({m['entry_n']} measurable)"),
                 entry_label(m["entry_p50"])))
    rows.append((_("规模", "size"),
                 _(f"单笔均买 {usd(m['avg_buy_usd'])}", f"{usd(m['avg_buy_usd'])} per buy"),
                 _(f"跟单起步 ≤ {usd(m['size_cap'])}", f"start at ≤ {usd(m['size_cap'])}")
                 if m["size_cap"] else _("无法计算", "not computable")))
    rows.append((_("胜率", "win rate"),
                 _(f"{pct(m['winrate'])} 于 {m['token_num']} 个币 · 重亏占比 {pct(m['lt50_share'])}",
                   f"{pct(m['winrate'])} over {m['token_num']} tokens · {pct(m['lt50_share'])} heavy losses"),
                 _("会砍仓", "cuts losses") if m["lt50_share"] < 0.35 else _("不砍仓", "does not cut")))
    lab_w = max(dwidth(r[0]) for r in rows) + 2
    concl_w = max(dwidth(r[2]) for r in rows)
    # Reserve room for the conclusion column so the arrow stays alignable on every row.
    mid_w = min(max(dwidth(r[1]) for r in rows) + 2, COL - 2 - lab_w - 2 - concl_w)
    for lab, val, concl in rows:
        head = f"  {wpad(lab, lab_w)}"
        if dwidth(val) <= mid_w - 1:
            out.append(f"{head}{wpad(val, mid_w)}→ {concl}")
        else:
            # Value too long to share the row: value first, conclusion on the next line,
            # right-aligned under the same arrow column.
            put(out, head, val, hang=2 + lab_w)
            out.append(" " * (2 + lab_w + mid_w) + f"→ {concl}")
    if m["hold_conflict"]:
        put(out, "  ⚠️ ", m["hold_conflict"])
    if m["one_coin_note"]:
        put(out, "  ⚠️ ", m["one_coin_note"])
    if m["pcr"] is not None and m["pcr_trusted"]:
        out.append(_(f"  利润集中度 {pct(m['pcr'])}（最大盈利仓位占全部盈利）",
                     f"  profit concentration {pct(m['pcr'])} (largest winner's share of all gains)"))
    out.append("")

    # ── 盈亏分布 ──
    b = m["buckets"]
    peak = max(b.values()) or 1
    out.append(_(f"📉 盈亏分布（{m['token_num']} 个币，计币不计钱）",
                 f"📉 OUTCOME DISTRIBUTION ({m['token_num']} tokens — counts tokens, not dollars)"))
    for lab, k in ((">500%", "gt5"), ("200–500%", "x2_5"), ("0–200%", "x0_2"),
                   ("−50–0%", "n50_0"), ("<−50%", "lt_n50")):
        n = b[k]
        out.append(f"  {lab:<10} {n:>5}  " + ("█" * max(1, int(round(30 * n / peak))) if n else ""))
    out.append("")

    # ── 现在在干嘛 ──
    pe, pl = m["posture"]
    out.append(_("🔄 它现在在干嘛", "🔄 WHAT IT IS DOING NOW"))
    out.append(_(f"  {pe} {pl} · 24h 买 {usd(m['buy_usd_24h'])} / 卖 {usd(m['sell_usd_24h'])}",
                 f"  {pe} {pl} · 24h bought {usd(m['buy_usd_24h'])} / sold {usd(m['sell_usd_24h'])}"))
    if m["recent_buys"]:
        put(out, _("  24h 买入：", "  bought in 24h: "),
            ", ".join(f"{sym} {usd(v)}" for sym, v in m["recent_buys"]))
    if m["open_book"]:
        out.append(_(f"  持仓 {m['holdings_n']} 个 · 合计 {usd(m['open_value'])}",
                     f"  {m['holdings_n']} positions · {usd(m['open_value'])} total"))
        hp_syms = {x["sym"] for x in m["honeypots"]}
        for bk in m["open_book"]:
            tag = " 🍯" if bk["sym"] in hp_syms else ""
            out.append(f"    {wpad(bk['sym'] + tag, 14)}{usd(bk['usd']):>10}  {pct(bk['chg'], 0):>8}  "
                       + _(f"成本 {usd(bk['cost'])} · 卖 {bk['sells']} 次",
                           f"cost {usd(bk['cost'])} · {bk['sells']} sells"))
    else:
        out.append(_("  持仓：未取到（见数据缺口）", "  live book: unavailable (see data gaps)"))
    out.append("")

    # ── 下一步 ──
    out.append(_("✅ 下一步", "✅ WHAT TO DO NEXT"))
    for a in actions(m, g):
        put(out, "  • ", a)
    out.append("")

    put(out, "", _(f"样本  activity {m['sampled']:,} 条 / {m['distinct_tokens_sampled']} 个币 · 覆盖 {m['span_h']:.1f} 小时"
                 + ("（触到分页上限，只覆盖最活跃的一段）" if m["hit_limit"] else ""),
                 f"sample  {m['sampled']:,} activity rows / {m['distinct_tokens_sampled']} tokens · "
                   f"spans {m['span_h']:.1f}h" + (" (hit page cap — busiest slice only)" if m["hit_limit"] else "")))
    if gaps:
        out.append(_("数据缺口（未评估 ≠ 通过）：", "DATA GAPS (unevaluated ≠ passed):"))
        for gp in gaps:
            put(out, "  ⚪ ", gp)
    out.append("")
    put(out, "", _("以上全部是已发生行为的度量，不是预测，也不是投资建议。",
                   "Everything above measures behaviour that already happened. "
                   "Not a prediction, not advice."))
    return "\n".join(out)


def actions(m, g):
    a = []
    p = {k: v[0] for k, v in g.items()}
    if m["trades"] == 0:
        return [
            _(
                "先确认你给的是钱包地址而不是代币合约；如果确实是钱包，等它有真实买卖记录再看。",
                "Confirm this is a wallet, not a token contract. If it is a wallet, wait for real trades.",
            )
        ]
    if m["recent_buys"]:
        syms = ", ".join(s for s, _v in m["recent_buys"][:3])
        a.append(
            _(
                f"它 24h 内买的是 {syms} —— 用 gmgn-token / gmgn-holder-analysis 单独查这几个币的筹码，别只因为它买了就买。",
                f"It bought {syms} in the last 24h — run gmgn-token / gmgn-holder-analysis on those before following it in.",
            )
        )
    if p["G3"] is False:
        a.append(
            _(
                "别抄单。把它当信号源：它买什么、在什么市值买，自己二次筛选后按自己的节奏进。",
                "Do not mirror it. Treat it as a signal source: note what and at what mcap, then enter on your own terms.",
            )
        )
    elif p["G3"] is True and m["size_cap"]:
        a.append(
            _(
                f"起步规模 ≤ {usd(m['size_cap'])}（它自己单笔均 {usd(m['avg_buy_usd'])}；超过它的单笔规模，你的滑点会比它差）。"
                f"下单前用 gmgn-swap 看报价。",
                f"Start at ≤ {usd(m['size_cap'])} (it averages {usd(m['avg_buy_usd'])} per buy; above its own size your "
                f"slippage is worse than its). Quote through gmgn-swap before sending.",
            )
        )
    if p["G4"] is False:
        a.append(
            _(
                "自己设止损 —— 它不砍仓，你跟到底就是陪它归零。",
                "Set your own stop — it does not cut, and riding it to the end means riding it to zero.",
            )
        )
    if m["copy_window_s"] > 0:
        a.append(
            _(
                f"如果要跟，你的下单要落在它买入后 {dur(m['copy_window_s'])}以内，否则不要进。",
                f"If you copy it, your order must land within {dur(m['copy_window_s'])} of its buy — otherwise skip the trade.",
            )
        )
    if m["is_dev"]:
        a.append(
            _(
                "这是发币方。别评估它的“交易能力”，去查它历史发币的毕业率和安全性（gmgn-wallet-score 的 Dev 角度）。",
                "This is a launcher. Do not score its trading — check its launch survival and security record (gmgn-wallet-score, Dev angle).",
            )
        )
    if m["form"][1] in (_("退潮", "cooling off"), _("崩坏", "broken down")):
        a.append(
            _(
                "它的钱是过去赚的。7 天后再跑一次这份分析，看是回暖还是继续退。",
                "Its money is historical. Re-run this in 7 days to see whether form recovers or keeps sliding.",
            )
        )
    a.append(
        _(
            "想要跟单评分和延迟/滑点回测，接 gmgn-wallet-score；想要一句话风格标签，接 gmgn-wallet-style。",
            "For a copy-trade score and a latency/slippage backtest use gmgn-wallet-score; for a one-line style tag use gmgn-wallet-style.",
        )
    )
    return a


# ─────────────────────────── entry ───────────────────────────


def main(argv):
    global ZH
    args = [a for a in argv[1:]]
    latency_s, my_size, fixture = 3.0, None, None
    rest = []
    k = 0
    while k < len(args):
        if args[k] == "--latency" and k + 1 < len(args):
            latency_s = f(args[k + 1], 3.0)
            k += 2
        elif args[k] == "--size" and k + 1 < len(args):
            my_size = f(args[k + 1])
            k += 2
        elif args[k] == "--fixture" and k + 1 < len(args):
            fixture = args[k + 1]
            k += 2
        else:
            rest.append(args[k])
            k += 1

    lang = next((x for x in rest if x in ("zh", "en")), "zh")
    ZH = lang == "zh"
    rest = [x for x in rest if x not in ("zh", "en")]

    gaps = []
    if fixture:
        with open(fixture) as fh:
            d = json.load(fh)
        wallet = d.get("_wallet", "FIXTURE")
        chain = d.get("_chain", "sol")
        gaps += d.get("_gaps", [])
    else:
        if len(rest) < 2:
            print(__doc__)
            return 2
        wallet, chain = rest[0], rest[1]
        try:
            d = collect(chain, wallet, gaps)
        except Gap as e:
            print(
                _(
                    f"取数失败，无法出结论：{e}\n"
                    "先确认 gmgn-cli config --check 通过；429 请按提示的 reset 时间再试；"
                    "401/403 且凭证正确时先排查 IPv6（gmgn-cli 只走 IPv4）。",
                    f"Data pull failed, no verdict possible: {e}\n"
                    "Check `gmgn-cli config --check` first; on 429 wait for the stated reset; "
                    "on 401/403 with valid credentials check IPv6 (gmgn-cli is IPv4 only).",
                )
            )
            return 1

    m = compute(d, latency_s, my_size)
    g = gates(m)
    print(report(wallet, chain, m, g, gaps))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
