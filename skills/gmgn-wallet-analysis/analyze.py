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



# ─── language ────────────────────────────────────────────────────────────────
# English is the source of truth: every user-facing string in this file is written in
# English, and `lang/<code>.json` maps an English template to its translation. A key that
# is missing from the table falls back to English, which is always a correct answer — so a
# partial translation degrades into mixed language, never into a crash or a blank line.
#
# Templates use positional placeholders (`{0}`, `{1}`) rather than named ones, because the
# same value often reads in a different position in another language and the translator
# needs to be able to move it.
LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")
LANG_TABLE = {}


def load_lang(code):
    """Populate LANG_TABLE for `code`. Absent or unreadable table => English throughout."""
    LANG_TABLE.clear()
    if code == "en":
        return
    path = os.path.join(LANG_DIR, f"{code}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        LANG_TABLE.update({k: v for k, v in data.items() if isinstance(v, str)})


def joinsym(items):
    """Join a list of symbols with the locale's separator. Keyed explicitly rather than on
    the separator itself, because ", " is too generic to be safe as a table key."""
    return LANG_TABLE.get("__list_separator__", ", ").join(items)


def T(en, *args):
    """Translate an English template and interpolate. `en` is the table key verbatim."""
    tpl = LANG_TABLE.get(en, en)
    if not args:
        return tpl.replace("{{", "{").replace("}}", "}")
    try:
        return tpl.format(*args)
    except (IndexError, KeyError, ValueError):
        # A translation with the wrong placeholders must not take the report down.
        return en.format(*args)


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
        return T('unknown')
    if sec < 60:
        return f"{sec:.0f}{T('s')}"
    if sec < 3600:
        return f"{sec / 60:.0f}{T('m')}"
    if sec < 86400:
        return f"{sec / 3600:.1f}{T('h')}"
    return f"{sec / 86400:.1f}{T('d')}"


def med(xs):
    return statistics.median(xs) if xs else 0.0


# Codepoints that occupy two terminal columns, and the ones that occupy none. The naive
# `ord(c) > 0x2E7F` test this replaced was wrong in both directions: it counted a variation
# selector (U+FE0F) as two columns, so `⚙️` measured 3 and every column it appeared in was
# padded short, and it counted the U+2600-27BF emoji (`⚡ ⚪ ✅`) as one, so lines carrying
# them could exceed COL in a real terminal while the width check called them safe.
ZERO_WIDTH = frozenset({0x200B, 0x200C, 0x200D, 0xFE0E, 0xFE0F, 0x20E3})
WIDE_RANGES = (
    (0x1100, 0x115F),      # Hangul Jamo
    (0x2E80, 0x303E),      # CJK radicals, Kangxi, CJK punctuation
    (0x3041, 0x33FF),      # kana, Hangul compat, CJK compat
    (0x3400, 0x4DBF),      # CJK ext A
    (0x4E00, 0x9FFF),      # CJK unified
    (0xA000, 0xA4CF),      # Yi
    (0xAC00, 0xD7A3),      # Hangul syllables
    (0xF900, 0xFAFF),      # CJK compat ideographs
    (0xFE30, 0xFE6F),      # CJK compat forms
    (0xFF00, 0xFF60),      # fullwidth forms
    (0xFFE0, 0xFFE6),      # fullwidth signs
    (0x1F300, 0x1FAFF),    # emoji: pictographs through symbols-and-pictographs-ext-A
    (0x1F000, 0x1F0FF),    # mahjong, dominoes, cards
    (0x1F100, 0x1F2FF),    # enclosed alphanumeric/ideographic supplement
    (0x2B00, 0x2BFF),      # misc symbols and arrows
)
# Emoji-presentation glyphs below U+2E80 that render wide. Enumerated rather than taken as a
# range because U+2600-27BF mixes wide emoji with narrow dingbats (`✓` is one column), and
# U+2500-257F box drawing — the report's own rules and bars — must stay one column.
WIDE_SYMBOLS = frozenset({
    0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3,
    0x25FD, 0x25FE, 0x2614, 0x2615, 0x2648, 0x2649, 0x264A, 0x264B, 0x264C,
    0x264D, 0x264E, 0x264F, 0x2650, 0x2651, 0x2652, 0x2653, 0x267F, 0x2693,
    0x26A1, 0x26AA, 0x26AB, 0x26BD, 0x26BE, 0x26C4, 0x26C5, 0x26CE, 0x26D4,
    0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705, 0x270A, 0x270B,
    0x2728, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757, 0x2795, 0x2796,
    0x2797, 0x27B0, 0x27BF, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55,
})


def cwidth(cp):
    """Terminal columns for one codepoint: 0, 1 or 2."""
    if cp in ZERO_WIDTH or 0x0300 <= cp <= 0x036F:
        return 0
    if cp in WIDE_SYMBOLS:
        return 2
    for lo, hi in WIDE_RANGES:
        if lo <= cp <= hi:
            return 2
    return 1


def is_emoji_cp(cp):
    """Part of an emoji glyph — used to keep a wrap from splitting a glyph from its label."""
    return (cp in WIDE_SYMBOLS
            or 0x1F000 <= cp <= 0x1FAFF
            or 0x2600 <= cp <= 0x27BF
            or 0x2B00 <= cp <= 0x2BFF)


def dwidth(s):
    """Display width in terminal columns.

    A base character followed by U+FE0F is an emoji-presentation sequence and renders two
    columns wide whatever the base would measure alone — that is what the selector means, so
    handling it here removes the need to enumerate `⚙ ⚔ ✂ ✈ ...` one by one. U+FE0E is the
    opposite request (text presentation) and stays narrow.
    """
    total, i, n = 0, 0, len(s)
    while i < n:
        cp = ord(s[i])
        nxt = ord(s[i + 1]) if i + 1 < n else 0
        if nxt == 0xFE0F:
            total += 2
            i += 2
            continue
        if nxt == 0xFE0E:
            total += 1
            i += 2
            continue
        total += cwidth(cp)
        i += 1
    return total


NATIVE = {"sol": "SOL", "bsc": "BNB", "eth": "ETH", "base": "ETH", "arc": "ARC"}


def usd_exact(v):
    """Whole dollars with separators — no K/M abbreviation.

    The card's headline exists because "$1,000 -> $1,621" needs no conversion to land.
    Run it through usd() and it becomes "$1.0K -> $1.6K", which is both a rounding of the
    thing being demonstrated and a return to the abstraction the card was built to avoid.
    """
    return f"${v:,.0f}"


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
    after_emoji = False
    prev_w = 0
    for ch in text:
        cp = ord(ch)
        # Same emoji-presentation rule as dwidth(): U+FE0F promotes the glyph before it to a
        # full two columns. Measuring it as zero here is what let a line reach 78 columns.
        w = max(0, 2 - prev_w) if cp == 0xFE0F else cwidth(cp)
        prev_w = w if cp != 0xFE0F else 2
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
            # A space directly after an emoji is NOT a break opportunity: the glyph labels
            # the phrase that follows it, and breaking there left lines ending in a bare
            # "✂️" with its name orphaned on the next line. Falling through leaves the
            # previous "·" as the break point, which is the one a reader wants.
            # Tracked as a flag rather than inspected from `cur`, because an emoji sequence
            # can end in a zero-width selector and its base char may not be wide on its own.
            if not (ch == " " and after_emoji):
                brk = len(cur) - 1
        if cp == 0xFE0F or is_emoji_cp(cp):
            after_emoji = True
        elif cp != 0x200D:
            after_emoji = False
    if cur:
        lines.append(cur)
    return lines or [""]


def put(out, prefix, text, hang=None):
    """Append `prefix + text`, wrapped, with continuation lines hanging under the text."""
    ind = " " * (dwidth(prefix) if hang is None else hang)
    # Budget for the WIDER of the two indents. A `hang` larger than the prefix would
    # otherwise push every continuation line past COL — the one way a caller could break
    # the width rule while still going through put().
    body = wrap(str(text), COL - max(dwidth(prefix), len(ind)))
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
    'wash_trader': ('🚩', 'veto_g1', 'wash trader',
     'P&L may be self-dealt, not market-earned'),
    'sandwich_bot': ('🥪', 'veto_g3', 'sandwich bot',
     'its profit comes from sandwiching orders like yours'),
    'mev_bot': ('🥪', 'veto_g3', 'MEV bot',
     'profit comes from ordering power, not token selection'),
    'rat_trader': ('🐀', 'warn', 'rat trader',
     'typically front-runs launches it is close to'),
    'bundler': ('📦', 'warn', 'bundler',
     'builds its position in the launch block'),
    'sniper': ('🎯', 'warn', 'sniper',
     'enters far too early for you to match its price'),
    'insider': ('🕵️', 'warn', 'insider',
     'an information edge you cannot replicate'),
    'dev': ('🏭', 'warn', 'token creator',
     'trades tokens it launched itself'),
    'kol': ('📣', 'warn', 'KOL',
     'a caller — you are probably not the first one in'),
    'top_followed': ('👥', 'warn', 'heavily followed',
     'copy flow already moved the price; your slippage is worse'),
    'top_renamed': ('🎭', 'warn', 'renamed repeatedly',
     'identity keeps churning; past reputation does not carry'),
    'fresh_wallet': ('🆕', 'warn', 'fresh wallet',
     'no history to check'),
    'smart_money': ('⭐', 'good', 'smart money',
     "GMGN's own positive marker"),
    'bluechip_owner': ('💎', 'good', 'bluechip holder',
     'has held assets that survived'),
    'whale': ('🐋', 'neutral', 'whale',
     'operates at a size that does not transfer to you'),
    'gmgn': ('🔧', 'neutral', 'GMGN user',
     'trades through GMGN — no risk meaning'),
    'photon': ('🔧', 'neutral', 'Photon user',
     'order channel'),
    'bullx': ('🔧', 'neutral', 'BullX user',
     'order channel'),
    'maestro': ('🔧', 'neutral', 'Maestro bot user',
     'order channel'),
    'pepeboost': ('🔧', 'neutral', 'PepeBoost user',
     'order channel'),
}


def read_tags(raw_tags):
    """Return [{key, emoji, sev, name, meaning, known}] — unknown tags kept verbatim."""
    out = []
    for t in raw_tags or []:
        key = str(t).strip()
        row = TAGS.get(key.lower())
        if row:
            emoji, sev, name, meaning = row
            out.append({"key": key, "emoji": emoji, "sev": sev,
                        "name": T(name), "meaning": T(meaning), "known": True})
        else:
            out.append({"key": key, "emoji": "❔", "sev": "neutral",
                        "name": f"`{key}`",
                        "meaning": T('unrecognised tag, shown verbatim, not used in any gate'),
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
            T('activity empty — copy window, entry band and scale-in/out shape were not evaluated')
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
            gaps.append(T('holdings came back empty — live book, profit concentration and the honeypot check were all skipped'))
    except Gap as e:
        d["holdings"] = []
        # Attribute the failure to its actual cause. This branch used to hardcode the
        # missing-credential wording for every failure, so a rate-limit refusal told the
        # reader to go and configure a key they had already configured — the wrong
        # instruction, and it hid the real reason. Anything that is not recognisably a
        # limiter refusal is reported verbatim rather than guessed at.
        txt = str(e)
        if "429" in txt or "RATE_LIMIT" in txt:
            gaps.append(
                T('holdings refused by the rate limiter (not an auth problem): {0} — profit concentration falls back to bucket inference; live book and honeypot check missing. Re-run once the limit resets.', e)
            )
        elif "PRIVATE_KEY" in txt or "signature" in txt.lower() or "401" in txt or "403" in txt:
            gaps.append(
                T('holdings unavailable (needs GMGN_PRIVATE_KEY / critical auth): {0} — profit concentration falls back to bucket inference; live book and honeypot check missing', e)
            )
        else:
            gaps.append(
                T('holdings failed: {0} — profit concentration falls back to bucket inference; live book and honeypot check missing', e)
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
    # The 0-200% bucket and the win rate disagree, and the report used to print both without
    # saying so. A live wallet showed 188 of 209 tokens in that bucket next to a 23.9% win
    # rate: 188 would imply 90%. Only one reading satisfies both numbers — the band absorbs
    # every token with no realized result yet (bought, not yet sold => realized ROI 0, which
    # sits on that band's lower edge), so its size is not a count of wins. `unsettled` is
    # that difference, and it is stated rather than left for the reader to notice.
    m["implied_winners"] = round(m["winrate"] * m["token_num"])
    m["unsettled"] = max(0, m["buckets"]["x0_2"] - m["implied_winners"])
    m["dist_gap"] = (
        m["token_num"] >= 20
        and m["buckets"]["x0_2"] > 0
        and m["unsettled"] >= 0.25 * m["buckets"]["x0_2"]
    )
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
        m["form"] = ("⚪", T('cannot tell'))
    elif ra <= 0 and r7 <= 0:
        m["form"] = ("⚫", T('never worked'))
    elif ra > 0.1 and r7 <= -0.1:
        m["form"] = ("💀", T('broken down'))
    elif r7 > max(0.1, ra):
        m["form"] = ("🔥", T('heating up'))
    elif abs(r7 - ra) <= 0.15:
        m["form"] = ("➡️", T('steady'))
    elif r7 < ra - 0.15:
        m["form"] = ("❄️", T('cooling off'))
    else:
        m["form"] = ("➡️", T('steady'))

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

    # Concentration of buy spend across tokens, and clustering in the day. Both are
    # activity-derived, so both are meaningless on a short sample: the top 3 of 3 tokens is
    # 100% by arithmetic, and any sample spanning under 12 hours "clusters" inside a 6-hour
    # window by arithmetic too. A live run fired both on a 14-hour sample with no warning.
    buy_by_tok = {}
    for a in trade_rows:
        if ev_type(a) == "buy" and tok_addr(a):
            buy_by_tok[tok_addr(a)] = buy_by_tok.get(tok_addr(a), 0.0) + f(a.get("cost_usd"))
    tot_buy = sum(buy_by_tok.values())
    m["top3_buy_share"] = (
        safe_div(sum(sorted(buy_by_tok.values(), reverse=True)[:3]), tot_buy)
        if tot_buy > 0 and len(buy_by_tok) >= 5 else None
    )
    hours = [0] * 24
    for t in ts:
        hours[int((t // 3600) % 24)] += 1
    if len(ts) >= 20 and m["span_h"] >= 12:
        best = max(sum(hours[(h + k) % 24] for k in range(6)) for h in range(24))
        m["hour_peak_share"] = safe_div(best, len(ts))
    else:
        m["hour_peak_share"] = None

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
        m["posture"] = ("😴", T('quiet for 24h'))
    elif s24 > 2 * b24:
        m["posture"] = ("📤", T('distributing'))
    elif b24 > 2 * s24:
        m["posture"] = ("🧊", T('accumulating'))
    else:
        m["posture"] = ("🔁", T('rotating'))

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
        m["one_coin_note"] = T('of {0} tokens only {1} cleared 2x while {2} lost money, yet the wallet is up {3} — the profit came from that one token', m['token_num'], big, losers, usd(m['realized_7d']))

    # ── position scale, from holdings ────────────────────────────────────────────
    # `avg_buy_usd` measures the CLIP, not the POSITION. A wallet that ladders a $54K
    # position together out of $3.4K clips reads as a $3.4K trader on clip size alone, and
    # a live run duly labelled exactly that wallet "ordinary, no distinguishing marks".
    # Position size and buys-per-position are the honest markers, and holdings carries
    # both — `history_total_buys` is the wallet's whole history on that token, not the
    # 300-row activity slice.
    m["top_pos_usd"] = None
    m["med_buys_per_pos"] = None
    if h:
        vals = sorted((f(x.get("usd_value")) for x in h), reverse=True)
        if vals and vals[0] > 0:
            m["top_pos_usd"] = vals[0]
        # Median over the WHOLE book is dominated by one-and-done dust positions, which
        # says nothing about how the wallet builds the positions it cares about. Take the
        # five largest by value — laddering is a property of size positions.
        top = sorted(h, key=lambda x: -f(x.get("usd_value")))[:5]
        bpp = sorted(b for b in (i(h_get(x, "history_total_buys", "buy_tx_count")) for x in top) if b > 0)
        if len(bpp) >= 3:
            m["med_buys_per_pos"] = bpp[len(bpp) // 2]

    # ── wash-trade corroboration ────────────────────────────────────────────────
    # `wash_trader` is a third-party heuristic label, not a finding. On this dataset it
    # fires on any wallet that round-trips a low-liquidity token many times — including a
    # $1K sliver of tokenised-stock churn on a wallet whose actual P&L is six-figure
    # memecoin positions. Obeying the tag alone mis-classified exactly that wallet as
    # un-copyable. So the tag now has to be corroborated against behaviour before it can
    # veto anything, and the corroboration is a single number.
    #
    # A position carries a GENUINE edge when its realized profit exceeds its own cost
    # basis, or clears $1,000 net per exit. Self-dealing cannot manufacture either: wash
    # volume nets to roughly zero minus fees, so its per-exit figure is small or negative.
    # `conviction_share` is the fraction of all realized gains that came from such
    # positions. High share → the record is NOT explained by round-tripping.
    m["conviction_share"] = None
    m["conviction_top"] = []
    if h:
        gains, conv, conv_syms = 0.0, 0.0, []
        for x in h:
            # `realized_profit` is the right numerator — a wash trader's closed loops are
            # what the tag is about. Fall back to `total_profit` only when the row omits it.
            rp = f(h_get(x, "realized_profit", "total_profit"))
            if rp <= 0:
                continue
            gains += rp
            cost = f(h_get(x, "accu_cost", "cost", "history_bought_cost"))
            sells = i(h_get(x, "history_total_sells", "sell_tx_count"))
            per_exit = safe_div(rp, sells) if sells > 0 else rp
            if (cost > 0 and rp >= cost) or per_exit >= 1000:
                conv += rp
                conv_syms.append(((x.get("token") or {}).get("symbol") or "?", rp))
        if gains > 0:
            m["conviction_share"] = conv / gains
            m["conviction_top"] = sorted(conv_syms, key=lambda kv: -kv[1])[:3]

    # ── where the money came from ───────────────────────────────────────────────
    # "It made 15.8%" does not tell a reader whether the edge is speed or selection, and
    # those two are copied in completely different ways: you cannot out-click a 288-trade/day
    # sniper, but you can wait and buy what a conviction wallet just laddered into. So
    # attribute the gains before interpreting them.
    #   gain_top3_share — do a handful of winners carry it, or is it spread thin?
    #   med_gain_per_exit — is each exit worth taking, or is this volume grinding?
    m["gain_top3_share"] = None
    m["med_gain_per_exit"] = None
    if h:
        wins = []
        for x in h:
            rp = f(h_get(x, "realized_profit", "total_profit"))
            if rp <= 0:
                continue
            sells = i(h_get(x, "history_total_sells", "sell_tx_count"))
            wins.append((rp, safe_div(rp, sells) if sells > 0 else rp))
        if wins:
            tot = sum(w[0] for w in wins)
            top3 = sum(sorted((w[0] for w in wins), reverse=True)[:3])
            m["gain_top3_share"] = safe_div(top3, tot)
            pe = sorted(w[1] for w in wins)
            m["med_gain_per_exit"] = pe[len(pe) // 2]

    # If a wash-trading tag is present but the gains demonstrably come from positions with
    # a real net edge, demote the tag in place: it stays visible as a warning with the
    # number that refuted it, and it no longer vetoes G1. Mutating `tag_info` here means
    # every render site downstream follows automatically.
    m["wash_refuted"] = None
    cs = m["conviction_share"]
    if cs is not None and cs >= 0.5:
        for t in m["tag_info"]:
            if t["sev"] == "veto_g1":
                m["wash_refuted"] = {"share": cs, "tag": t["name"]}
                t["sev"] = "warn"
                # A ✅-adjacent glyph would be wrong (the label is real, and it is telling
                # you something about the wallet's churn) but so is 🚩 next to a sentence
                # saying the flag does not hold. ❔ is the honest one.
                t["emoji"] = "❔"
                t["name"] = T('{0} (refuted)', t['name'])
                t["meaning"] = T("GMGN's label, refuted locally: {0} of realized gains came from positions netting more than their own cost basis — self-dealing cannot produce that", pct(cs))

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
    # Fields `portfolio stats` returns that nothing read. Each answers a question a reader
    # asks out loud and the report could not previously answer.
    #   native_balance — the dry powder. GMGN's own leaderboard puts it in column two.
    #   last_timestamp — freshness. A wallet last active three days ago is not the same
    #                    wallet as one trading right now, and every other figure here is
    #                    silent about which it is.
    m["native_balance"] = f(s7.get("native_balance"))
    last_ts = f(s7.get("last_timestamp"))
    m["idle_s"] = max(0.0, time.time() - last_ts) if last_ts > 0 else None
    m["stale"] = m["idle_s"] is not None and m["idle_s"] > 48 * 3600

    m["net_per_sell"] = safe_div(m["realized_7d"], m["sell"])

    # `portfolio stats` reports the fees actually paid in the window — `bought_fee` and
    # `sold_fee` — and this used to ignore both, estimating friction instead from the gas
    # median of a 300-row activity sample times the trade count. On a live wallet the
    # estimate said gas ate 0.0% of the profit while the real fees were $4,408 against
    # $167,237 realized, i.e. 2.6%. Two orders of magnitude, and the exact figure was in a
    # response already in hand. The estimate stays as the fallback for chains or versions
    # that omit the fee fields, and the report says which one it is showing.
    m["fee_total"] = f(s7.get("bought_fee")) + f(s7.get("sold_fee"))
    m["fee_exact"] = m["fee_total"] > 0
    if m["fee_exact"] and m["realized_7d"] > 0:
        m["gas_drag"] = m["fee_total"] / m["realized_7d"]
        m["gas_total_est"] = m["fee_total"]
    elif m["avg_gas_usd"] > 0 and m["trades"] > 0 and m["realized_7d"] > 0:
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
            m["hold_conflict"] = T("the API's average hold is {0}, but the median first-buy→first-sell in the live sample is {1} — the mean is dragged up by bags it never sold. Read the median, not the mean", dur(m['avg_hold_s']), dur(m['copy_window_s']))

    # ── honeypots in the live book ──
    # `token.is_honeypot` ships inline on every holdings row, so this costs nothing and is
    # available whenever holdings is. `security_checked` records how many rows actually
    # carried the flag, so a missing flag is never read as "clean".
    hp_names, flagged, hp_refuted = [], 0, []
    for h_row in (d.get("holdings") or []):
        tk = h_row.get("token") or {}
        if tk.get("is_honeypot") is None:
            continue
        flagged += 1
        if not _b(tk.get("is_honeypot")):
            continue
        sym = tk.get("symbol") or (tok_addr(h_row) or "?")[:6]
        sells = i(h_get(h_row, "history_total_sells", "sell_tx_count"))
        # A honeypot is a token you CANNOT sell. When the same row records completed sells,
        # the flag is contradicted by this wallet's own history — the usual cause is a
        # transfer-restricted RWA / tokenised-stock contract that trips naive sell
        # simulators. A live run failed G4 on seven such "honeypots", one of which this
        # wallet had sold 101 times. The refutation is free: it is on the same row.
        if sells > 0:
            hp_refuted.append({"sym": sym, "sells": sells})
            continue
        hp_names.append({"sym": sym, "usd": f(h_row.get("usd_value"))})
    m["honeypots"] = hp_names
    m["honeypot_usd"] = sum(x["usd"] for x in hp_names)
    m["hp_refuted"] = hp_refuted
    m["security_checked"] = flagged

    # Where it hunts — launchpad mix across the live book, also inline on token.
    lp = {}
    for h_row in (d.get("holdings") or []):
        name = ((h_row.get("token") or {}).get("launchpad_platform")
                or (h_row.get("token") or {}).get("launchpad"))
        if name:
            lp[str(name)] = lp.get(str(name), 0) + 1
    m["launchpads"] = sorted(lp.items(), key=lambda kv: -kv[1])[:3]

    # ── the decision card's numbers ──────────────────────────────────────────────
    # "+62.1%" is a ratio a reader has to convert before it means anything. The same fact
    # told as money needs no conversion: $1,000 -> $1,621. Nothing new is fetched; this is
    # roi_7d wearing clothes a newcomer already owns.
    m["story_stake"] = 1000.0
    m["story_out"] = (1000.0 * (1.0 + m["roi_7d"])) if m["roi_7d"] is not None else None
    # How much hotter than its own baseline. Only meaningful when the baseline is positive:
    # against a negative or ~zero all-time ROI the ratio is noise, so it is dropped rather
    # than printed as a huge multiple that means nothing.
    m["pace_x"] = None
    if m["roi_7d"] is not None and m["roi_all"] is not None and m["roi_all"] >= 0.02:
        r = m["roi_7d"] / m["roi_all"]
        if r >= 1.5:
            m["pace_x"] = r

    # size guidance
    # The size the reader intends, checked against the wallet's own clip. Above the wallet's
    # own size your slippage is worse than its, so its results stop describing you — which
    # is the whole reason size_cap exists. Stating the multiple makes that concrete.
    m["my_size"] = my_size
    m["size_ratio"] = (my_size / m["avg_buy_usd"]) if (my_size and m["avg_buy_usd"] > 0) else None
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
        blank = T('no buys or sells in 7 days — nothing to evaluate')
        return {k: (None, blank) for k in ("G1", "G2", "G3", "G4")}

    # G1 AUTHENTICITY — a wash-trading marker outranks every other test here. If the
    # volume may be self-dealt, the win rate, the ROI and the bucket distribution are all
    # measuring the wallet trading against itself, and no amount of good-looking
    # distribution rescues that.
    wash = [t for t in m["tag_info"] if t["sev"] == "veto_g1"]
    if wash and m["conviction_share"] is None:
        # Tag present and uncheckable. This is exactly the ⚪ case: "we could not verify" is
        # not "confirmed fake", and it is not "fine" either. Do not manufacture a ❌.
        names = joinsym(t["name"] for t in wash)
        g["G1"] = (
            None,
            T('GMGN flags this wallet as {0}, and it cannot be checked (holdings unavailable) — the {1} in this window is neither confirmed nor refuted. Configure GMGN_PRIVATE_KEY and re-run', names, usd(m['realized_7d'])),
        )
    elif wash:
        names = joinsym(t["name"] for t in wash)
        g["G1"] = (
            False,
            T('GMGN flags this wallet as {0}, and the local check agrees: only {1} of realized gains came from positions netting more than their own cost basis — the rest is round-tripped volume. The {2} realized P&L cannot be taken at face value', names, pct(m['conviction_share']), usd(m['realized_7d'])),
        )
    elif m["is_dev"]:
        g["G1"] = (
            False,
            T('launcher wallet: created {0} vs traded {1} — its win rate and entry timing are self-authored, not a market read', m['created_tokens_n'], m['token_num']),
        )
    elif m["token_num"] < 5:
        g["G1"] = (
            False,
            T('only {0} tokens — no ratio computed on this is meaningful', m['token_num']),
        )
    elif m["one_coin_note"]:
        g["G1"] = (False, m["one_coin_note"])
    elif m["pcr_trusted"] and m["pcr"] >= 0.75:
        g["G1"] = (
            False,
            T('profit concentration {0} (across {1} positions) — one coin carried the record', pct(m['pcr']), m['holdings_n']),
        )
    else:
        if m["pcr_trusted"]:
            pcr_txt = T('profit concentration {0}', pct(m['pcr']))
        elif m["pcr"] is not None:
            pcr_txt = T('profit concentration {0} (only {1} positions — too thin to rely on)', pct(m['pcr']), m['holdings_n'])
        else:
            pcr_txt = T('profit concentration not measured (holdings unavailable)')
        detail = [T('{0} tokens, {1} profitable, {2}', m['token_num'], m['winners'], pcr_txt)]
        if m["wash_refuted"]:
            top = joinsym(sym for sym, _v in m["conviction_top"])
            detail.append(T('GMGN carries a "{0}" flag; the local check refutes it: {1} of realized gains came from size positions like {2} that netted more than their own cost basis. Self-dealing cannot produce that — the flag is downgraded to a caution, not a veto', m['wash_refuted']['tag'], pct(m['wash_refuted']['share']), top))
        g["G1"] = (True, detail)

    # G2 CURRENCY
    emoji, label = m["form"]
    r7 = m["roi_7d"]
    ra = m["roi_all"]
    r7t = pct(r7) if r7 is not None else "n/a"
    rat = pct(ra) if ra is not None else "n/a"
    if label in (T('broken down'), T('never worked')):
        g["G2"] = (
            False,
            T('{0} {1}: 7d {2} vs all-time {3}', emoji, label, r7t, rat),
        )
    elif r7 is not None and r7 <= 0 and m["roi_30d"] is not None and m["roi_30d"] <= 0:
        g["G2"] = (
            False,
            T('both 7d and 30d are negative ({0} / {1})', r7t, pct(m['roi_30d'] or 0)),
        )
    else:
        g["G2"] = (
            True,
            T('{0} {1}: 7d {2} vs all-time {3}', emoji, label, r7t, rat),
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
                T('median copy window {0} against your {1} latency — under 3x margin, it is likely already selling when you land', dur(cw), dur(lat))
            )
        else:
            reasons_ok.append(
                T('copy window {0} (your latency budget {1})', dur(cw), dur(lat))
            )
    if m["entry_n"] >= 5:
        if m["entry_p50"] > 0 and m["entry_p50"] < 30_000:
            reasons_fail.append(
                T('median entry mcap {0} — sniper/pre-graduation territory; you enter at 5–10x its cost. {1} of its entries are under $100k', mc(m['entry_p50']), pct(m['entry_sub100k']))
            )
        else:
            reasons_ok.append(
                T('entry mcap p25/p50/p75 = {0}/{1}/{2} · {3} of entries under $100k',
                  mc(m['entry_p25']), mc(m['entry_p50']), mc(m['entry_p75']), pct(m['entry_sub100k']))
            )
    for t in m["tag_info"]:
        if t["sev"] == "veto_g3":
            reasons_fail.append(T('GMGN flags it as {0} — {1}', t['name'], t['meaning']))
    if m["followers"] >= 10_000 and (m["entry_p50"] == 0 or m["entry_p50"] < 1_000_000):
        reasons_fail.append(
            T('a public identity with {0:,} followers trading small caps — copy flow has already moved the price before your order', m['followers'])
        )
    # Gas that eats a large share of the per-trade net leaves nothing for your slippage.
    if m["gas_drag"] is not None and m["gas_drag"] >= 0.25:
        reasons_fail.append(
            T('fees took {0} of the profit ({1} paid vs {2} realized), leaving {3} net per trade — no room for your slippage', pct(m['gas_drag']), usd(m['gas_total_est']), usd(m['realized_7d']), usd(m['net_per_sell']))
            if m["fee_exact"] else
            T('gas is an estimated {0} of the profit ({1:,} trades × {2} ≈ {3} vs {4} realized), leaving {5} net per trade — no room for your slippage', pct(m['gas_drag']), m['trades'], usd(m['avg_gas_usd']), usd(m['gas_total_est']), usd(m['realized_7d']), usd(m['net_per_sell']))
        )
    if m["avg_buy_usd"] > 0 and m["avg_buy_usd"] < 50:
        reasons_fail.append(
            T('average buy {0} — thin enough that fees and slippage eat the edge', usd(m['avg_buy_usd']))
        )
    if m["per_day"] > 100:
        reasons_fail.append(
            T('{0:,.0f} trades/day — bot cadence, no hand can keep pace', m['per_day'])
        )
    if m["copy_window_n"] < 3 and m["entry_n"] < 5:
        g["G3"] = (
            None,
            T('activity sample too thin — reachability not evaluated'),
        )
    elif reasons_fail:
        g["G3"] = (False, reasons_fail)
    else:
        g["G3"] = (True, reasons_ok or [T('no reachability obstacle found')])

    # G4 SURVIVABILITY
    if m["token_num"] < 5:
        g["G4"] = (None, T('sample too thin — survivability not evaluated'))
    elif len(m["honeypots"]) >= 2:
        syms = joinsym(x["sym"] for x in m["honeypots"])
        g["G4"] = (
            False,
            T('{0} live positions are honeypots ({1}, {2} that cannot be sold) — its own screening did not catch them, and copying it walks into the same ones', len(m['honeypots']), syms, usd(m['honeypot_usd'])),
        )
    elif m["lt50_share"] >= 0.35:
        g["G4"] = (
            False,
            T('{0} of its tokens are down >50% ({1}/{2}) — it does not cut', pct(m['lt50_share']), m['buckets']['lt_n50'], m['token_num']),
        )
    elif m["hold_to_zero"] is not None and m["hold_to_zero"] >= 3:
        g["G4"] = (
            False,
            T('{0} positions down 90%+ with zero sells — riding to zero is the habit', m['hold_to_zero']),
        )
    else:
        reasons = [
            T('heavy-loss share {0} ({1}/{2} down >50%)', pct(m['lt50_share']), m['buckets']['lt_n50'], m['token_num'])
        ]
        if m["hold_to_zero"] is not None:
            reasons.append(T('{0} ridden to zero (down 90%+ with zero sells)', m['hold_to_zero']))
        if m["security_checked"] and m.get("hp_refuted"):
            syms = joinsym(x["sym"] for x in m["hp_refuted"])
            mx = max(x["sells"] for x in m["hp_refuted"])
            reasons.append(T('honeypot flag checked on {0} positions: {1} hit ({2}) but each is refuted by its own fill history — one has {3:,} completed sells, and a honeypot cannot be sold. These are transfer-restricted tokenised-stock / RWA contracts — false positives', m['security_checked'], len(m['hp_refuted']), syms, mx))
        elif m["security_checked"]:
            reasons.append(T('honeypot flag checked on {0} positions, none hit', m['security_checked']))
        else:
            reasons.append(T('⚪ honeypot NOT checked (holdings unavailable) — this pass covers loss-cutting only, not honeypots'))
        g["G4"] = (True, reasons)

    # A launcher's entry timing and loss-cutting are measurements of its own token's
    # price, which it controls. Reporting them as ✅ would be reporting self-dealing
    # as skill — so they are marked unevaluated, not passed.
    if m["is_dev"]:
        na = T('launcher wallet — this measures its handling of its own token, so it does not apply')
        g["G3"] = (None, na)
        g["G4"] = (None, na)
    return g


def verdict(m, g):
    """Returns (emoji, headline, what-to-do).

    Language rules for this layer, which is the only part most readers finish:
      • The headline is a verb the reader can act on, then the cause in everyday words.
        Not "the record cannot be taken as evidence" (legalese) — "the profit is faked".
      • The action is ONE short imperative sentence. No sub-clauses, no hedging tail.
      • Colour means what it says: 🔴 measured and bad, 🟡 act differently, ⚪ not measured.
        An unmeasured gate must never render 🔴 — "we could not tell" is not "it is bad".
      • The action never restates the gate reason printed below it.
    """
    p = {k: v[0] for k, v in g.items()}

    if m["trades"] == 0:
        return ("⚪",
                T('NO READ · no trades in 7 days'),
                T('First confirm this is a wallet, not a token contract. Three checks below.'))

    if p["G1"] is False:
        if any(t["sev"] == "veto_g1" for t in m["tag_info"]):
            return ("🔴",
                    T('DO NOT COPY · the profit is self-dealt'),
                    T('Treat its P&L as if it were not there. Watch what it buys; do not use these numbers.'))
        if m["is_dev"]:
            return ("🔴",
                    T('DO NOT COPY · it is a launcher trading its own tokens'),
                    T('Do not read its trading. Check how many of its launches survived (gmgn-wallet-score).'))
        if m["one_coin_note"]:
            return ("🔴",
                    T('DO NOT COPY · one token made all the money'),
                    T('Come back when it has done it again on other tokens.'))
        # Too thin to measure is ⚪, not 🔴. Nothing bad was found — nothing was found.
        return ("⚪",
                T('NO READ · only {0} tokens traded', m['token_num']),
                T('The sample is too small for any ratio to hold. Watchlist it until it has traded 5.'))

    if p["G2"] is False:
        return ("🔴",
                T('DO NOT COPY · it has stopped making money'),
                T('Re-run in 7 days to see whether it recovers or keeps sliding.'))

    # G3 and G4 are independent problems. Reporting only the first one silently drops the
    # other — a wallet you cannot get filled on AND that never cuts needs both sentences.
    if p["G3"] is False and p["G4"] is False:
        return ("🟡",
                T('WATCH, DO NOT COPY · you cannot get its fills, and it never cuts'),
                T('Use it only as a signal of what to look at. If you enter, set your own stop.'))
    if p["G3"] is False:
        return ("🟡",
                T('WATCH, DO NOT COPY · you cannot get its fills'),
                T('Note what it buys and at what market cap, then enter on your own terms.'))
    if p["G4"] is False:
        return ("🟡",
                T('COPY THE BUYS, NOT THE EXITS · it does not cut losses'),
                T('Take its entries and keep your own stop. Do not wait for it to sell first.'))

    if p["G1"] is None:
        return ("🟡",
                T('HOLD OFF · a wash-trading flag we cannot check'),
                T('Configure GMGN_PRIVATE_KEY and re-run. Do not size off this record first.'))
    if p["G3"] is None or p["G4"] is None:
        return ("🟡",
                T('HOLD OFF · one of the four was not measured'),
                T('Fill in the missing data first — usually by configuring GMGN_PRIVATE_KEY.'))

    size = usd(m["size_cap"]) if m["size_cap"] else T('your normal size')
    win = dur(m["copy_window_s"]) if m["copy_window_s"] > 0 else None
    if win:
        act = T('Start at ≤ {0}, landing within {1} of its buy.', size, win)
    else:
        act = T('Start at ≤ {0}.', size)
    return ("🟢",
            T('COPYABLE AT SMALL SIZE · all four pass'),
            act)


# ─────────────────────────── report ───────────────────────────

GATE_NAMES = {
    "G1": "AUTHENTICITY",
    "G2": "CURRENCY",
    "G3": "REACHABILITY",
    "G4": "SURVIVABILITY",
}

GATE_GLOSS = {
    "G1": "is the data trustworthy",
    "G2": "is it still earning now",
    "G3": "can you get filled",
    "G4": "does it cut losses",
}


# The card states each gate as an outcome in words a newcomer already uses. The gate's own
# name ("AUTHENTICITY") and the number behind it stay on the evidence layer: naming the
# test invites the question "how did you test it", which is exactly what the card defers.
GATE_PLAIN = {
    "G1": ("record is real", "the track record is genuine, not manufactured"),
    "G2": ("earning now", "not living off an old run"),
    "G3": ("you can keep up", "its fills are reachable at your speed"),
    # Keyed "it cuts losses", not "cuts losses": that shorter string is already the
    # numbers panel's win-rate chip, and a table keyed on English text has exactly one slot
    # per string. Reusing it silently rewrote that chip in eight fixtures.
    "G4": ("it cuts losses", "it does not ride positions to zero"),
}


def mark(v):
    return {True: "✅", False: "❌", None: "⚪"}[v]


# ─── style layer: main title + speed subtitle ────────────────────────────────
# Merged in from the wallet-style testbench. Four deliberate changes were made on the
# way in, each because the original mis-labelled a wallet we had already verified:
#   1. No "officially verified" badge. It fired on any non-empty `common.tags`, so it printed
#      `wash_trader` under a commendation glyph. Tags go through TAGS/severity instead.
#   2. The speed subtitle reads the MEDIAN copy window, not `avg_holding_period`. The
#      mean counts bags never sold, so it called a 2-minute scalper a 1-7 day swing trader.
#   3. P5 needs ROI > 50% plus ONE of {win rate ≥ 50%, heavy-loss share < 15%}, not all
#      three. Memecoin P&L is low-hit-rate with a fat right tail; requiring 50% win rate
#      pushed a wallet sitting at #3 on GMGN's own 7D leaderboard down to P4.
#   4. Activity-derived badges are gated on sample size (see top3_buy_share / hour_peak).
# The `token_num >= 5` floor on P5 is kept as-is: one lucky coin must not score "one-shot".

TITLES = {
    ('L4', 'P5'): ('🖨️', 'money printer',
     'machine cadence and still strongly profitable'),
    ('L4', 'P4'): ('⚙️', 'full-auto grinder',
     'thin margins, huge volume'),
    ('L4', 'P3'): ('\U0001faab', 'worn down',
     'whatever it earns, fees and slippage take back'),
    ('L4', 'P2'): ('🔥', 'gas burner',
     'high frequency, high friction; the loss is mostly cost'),
    ('L4', 'P1'): ('💥', 'self-destruct',
     'machine cadence plus broad heavy losses'),
    ('L3', 'P5'): ('🌾', 'harvester',
     'high frequency and strongly profitable — the strongest cell'),
    ('L3', 'P4'): ('⚔️', 'active winner',
     'busy hands that keep the money'),
    ('L3', 'P3'): ('🌀', 'spinning top',
     'spinning fast, going nowhere'),
    ('L3', 'P2'): ('💸', 'fee donor',
     'real volume, and the money went on-chain'),
    ('L3', 'P1'): ('🩸', 'bleeding out',
     'charging in fast with a heavy tail of big losses'),
    ('L2', 'P5'): ('🦅', 'old hunter',
     'swings rarely, earns well — the most copyable rhythm'),
    ('L2', 'P4'): ('📈', 'steady hand',
     'normal cadence, positive return, no glaring weakness'),
    ('L2', 'P3'): ('☕', 'lukewarm',
     'active, but it has not turned into anything'),
    ('L2', 'P2'): ('🐑', 'retail loser',
     'the most common cell on the board'),
    ('L2', 'P1'): ('🕳️', 'deep underwater',
     'most of its coins are down more than 50%'),
    ('L1', 'P5'): ('🗡️', 'one-shot',
     'almost never trades, and lands it when it does'),
    ('L1', 'P4'): ('🧘', 'zen winner',
     'the gain came from picks, not from working the trades'),
    ('L1', 'P3'): ('👀', 'bystander',
     'too small a sample to mean much'),
    ('L1', 'P2'): ('💧', 'toe in the water',
     'tried a few times, none worked'),
    ('L1', 'P1'): ('⚰️', 'wiped out',
     'one or two swings, wiped out'),
}


def freq_level(per_day):
    """Same boundaries as cadence_label — one concept, one set of thresholds."""
    if per_day < 1:
        return "L1"
    if per_day < 10:
        return "L2"
    if per_day <= 50:
        return "L3"
    return "L4"


def pnl_level(m):
    """P5 requires ROI > 50% and ONE corroborating shape, not all three. See note above.

    Returns (level, basis) — `basis` names the corroborator that carried P5, so the title's
    "strongly profitable" is never a bare claim. A 33%-win-rate wallet reaching P5 on its
    must not be glossed as high-hit-rate.
    """
    roi = m["roi_7d"] if m["roi_7d"] is not None else 0.0
    hits = []
    if m["winrate"] >= 0.5:
        hits.append(T('{0} hit rate', pct(m['winrate'])))
    if m["lt50_share"] < 0.15:
        hits.append(T('only {0} heavy losses', pct(m['lt50_share'])))
    if roi > 0.5 and m["token_num"] >= 5 and hits:
        return ("P5", T('7d {0} + {1}', pct(roi), hits[0]))
    if roi > 0.1:
        return ("P4", None)
    if m["lt50_share"] >= 0.40 and m["realized_7d"] < 0:
        return ("P1", None)
    if abs(roi) <= 0.10:
        return ("P3", None)
    if m["realized_7d"] < 0 or roi < 0:
        return ("P2", None)
    return ("P3", None)


def style_title(m):
    """(emoji, name, gloss, cell). None when there is nothing to label."""
    # No label on a sample that cannot carry one. The verdict already reads "no read" for a
    # sub-5-token wallet; printing "steady hand - normal cadence, positive return, no glaring
    # weakness" next to it would contradict it. Silence is the honest label here.
    if m["trades"] == 0 or m["token_num"] < 5:
        return None
    plevel, basis = pnl_level(m)
    cell = (freq_level(m["per_day"]), plevel)
    e, name, gloss_en = TITLES[cell]
    gloss = T(gloss_en)
    if basis:
        gloss += T(' ({0})', basis)
    return (e, T(name), gloss, f"{cell[0]}×{cell[1]}")


def style_speed(m):
    """(emoji, name, range) from the MEDIAN copy window — never the mean hold."""
    if m["copy_window_n"] < 3 or m["copy_window_s"] <= 0:
        return None
    s = m["copy_window_s"]
    if s < 60:
        return ("⚡", T('flash flipper'), T('< 60s'))
    if s < 86_400:
        return ("🐇", T('intraday'), T('< 24h'))
    if s < 604_800:
        return ("🧭", T('swing'), T('1–7 days'))
    return ("💎", T('long hold'), T('> 7 days'))


def spray_tail(win):
    """The copy-window clause of the spray-and-hit engine. Hoisted out of the sentence so
    the sentence stays a single translatable template rather than a concatenation."""
    if win:
        return T('You would need to land inside {0} — not achievable by hand', win)
    return T('Copying it is a race on latency, not on judgement')


def profit_engine(m):
    """(chip, one line with the numbers, what it means for copying) or None.

    Three engines, separated by two independent numbers — trade cadence and how
    concentrated the gains are. The point is not the label: it is that "speed" and
    "selection" are copied differently, and a reader who cannot tell them apart will copy
    the wrong half. Needs `holdings`; returns None rather than guessing without it.
    """
    if m["conviction_share"] is None or m["gain_top3_share"] is None:
        return None
    fast = m["per_day"] >= 50
    concentrated = m["gain_top3_share"] >= 0.5
    conv = m["conviction_share"] >= 0.6
    win = dur(m["copy_window_s"]) if m["copy_window_n"] >= 3 else None

    if fast and concentrated:
        return (
            T('🕸️ spray-and-hit'),
            T('{0:,.0f} trades/day at {1} a clip, and the top 3 winners carry {2} of the profit', m['per_day'], usd(m['avg_buy_usd']), pct(m['gain_top3_share'])),
            T('the profit comes from volume of attempts times a few hits, not from picking well. {0}', spray_tail(win)),
        )
    if fast:
        return (
            T('⚙️ turnover grind'),
            T('{0:,.0f} trades/day with profit spread thin (top 3 = {1}), median {2} net per winning exit', m['per_day'], pct(m['gain_top3_share']), usd(m['med_gain_per_exit'])),
            T('the profit is volume, and each exit is too thin to survive your slippage and fees'),
        )
    if conv and concentrated:
        return (
            T('🎯 pick-and-size'),
            T('{0:,.0f} trades/day is not fast; {1} of gains came from positions netting more than their own cost, top 3 winners = {2}', m['per_day'], pct(m['conviction_share']), pct(m['gain_top3_share'])),
            T('the profit comes from picking right and then sizing up, not from speed — this is the kind you can follow a step behind'),
        )
    return (
        T('🧩 diffuse accumulation'),
        T('{0:,.0f} trades/day, gains neither concentrated (top 3 = {1}) nor speed-driven, median {2} per winning exit', m['per_day'], pct(m['gain_top3_share']), usd(m['med_gain_per_exit'])),
        T('no single profit engine — following it means following the whole book, not any one trade'),
    )


def archetype(m):
    """Say what kind of counterparty this is, before any number gets interpreted."""
    tags = []
    if m["is_dev"]:
        tags.append(T('🏭 launcher (marks its own homework)'))
    if m["per_day"] > 50:
        tags.append(T('🤖 bot-tier {0:,.0f} trades/day', m['per_day']))
    if m["entry_n"] >= 5 and 0 < m["entry_p50"] < 100_000:
        tags.append(T('🎯 sniper, median entry {0}', mc(m['entry_p50'])))
    if m["avg_buy_usd"] >= 10_000:
        tags.append(T('🐋 whale, {0} per buy', usd(m['avg_buy_usd'])))
    if m["age_days"] is not None and m["age_days"] < 30:
        tags.append(T('🆕 new wallet, {0:.0f} days old', m['age_days']))
    if m["flip5_rate"] >= 0.3:
        tags.append(T('⚡ 5-second flipper on {0} of round trips', pct(m['flip5_rate'])))
    if m["top_pos_usd"] and m["top_pos_usd"] >= 10_000:
        tags.append(T('🏦 size-position trader, largest holding {0}', usd(m['top_pos_usd'])))
    if m["med_buys_per_pos"] and m["med_buys_per_pos"] >= 10:
        tags.append(T('🧱 ladders its size positions, median {0:,} buys each', m['med_buys_per_pos'])
                    + (T(' over {0}', dur(m['accum_window_s'])) if m['accum_window_s'] > 0 else ""))
    elif m["avg_buys_per_token"] >= 3:
        tags.append(T('🧱 scales in, {0:.1f} buys/token', m['avg_buys_per_token']))
    # 🎰 low hit rate carried by one or two outsized wins — a different animal from a
    # wallet with the same ROI and an even distribution.
    if m["winrate"] < 0.35 and m["buckets"]["gt5"] >= 1 and m["token_num"] >= 5:
        tags.append(T('🎰 lottery profile, {0} hit rate but {1} tokens above 5x', pct(m['winrate']), m['buckets']['gt5']))
    if m["avg_sells_per_token"] >= 3:
        tags.append(T('✂️ scales out, {0:.1f} sells/token', m['avg_sells_per_token']))
    # Both of the next two are None unless the sample can carry them — see the metric.
    if m["top3_buy_share"] is not None and m["top3_buy_share"] >= 0.7:
        tags.append(T('📦 concentrated bets, top 3 tokens are {0} of buy spend', pct(m['top3_buy_share'])))
    if m["hour_peak_share"] is not None and m["hour_peak_share"] >= 0.7:
        tags.append(T('🌙 fixed hours, {0} of trades inside one 6-hour window', pct(m['hour_peak_share'])))
    if m["dump_share"] >= 0.7 and m["sampled"] >= 20:
        tags.append(T('💣 dumps in one go on {0} of exits', pct(m['dump_share'])))
    return tags


def roi_label(v):
    if v is None:
        return T('unknown')
    if v > 0.5:
        return T('strongly profitable')
    if v > 0.1:
        return T('net positive')
    if abs(v) <= 0.1:
        return T('flat')
    if v > -0.3:
        return T('net negative')
    return T('badly down')


def cadence_label(per_day):
    if per_day > 50:
        return T('bot-tier, unfollowable')
    if per_day > 10:
        return T('high freq, needs tooling')
    if per_day >= 1:
        return T('normal, hand-tradeable')
    return T('low freq, slow evidence')


def entry_label(p50):
    if p50 <= 0:
        return T('not measured')
    if p50 < 30_000:
        return T('pre-graduation, you pay up')
    if p50 < 100_000:
        return T('sniper range, no match')
    if p50 < 300_000:
        return T('small cap, heavy slippage')
    if p50 < 3_000_000:
        return T('mid cap, copyable')
    return T('large cap, deep')


def friction_label(m):
    if m["gas_drag"] is None:
        return T('not enough gas data to evaluate')
    if m["gas_drag"] >= 0.25:
        return T('friction eats the bulk')
    if m["gas_drag"] >= 0.10:
        return T('meaningful friction')
    return T('friction manageable')


def speed_read(m, g, why):
    """Three lines, each a finished thought. Nothing here requires the reader to compute."""
    rows = []
    marks = archetype(m)
    st, sp = style_title(m), style_speed(m)
    if st:
        head = f"{st[0]} {st[1]}"
        if sp:
            head += f" · {sp[0]} {sp[1]}"
        if marks:
            head += " · " + marks[0]
        rows.append((T('what it is'), head))
    else:
        rows.append((T('what it is'),
                     " · ".join(marks[:2]) if marks else T('ordinary trading wallet, no distinguishing marks')))
    key = []
    if m["per_day"] > 10:
        key.append(T('{0:,.0f} trades/day', m['per_day']))
    if m["gas_drag"] is not None and m["gas_drag"] >= 0.10:
        key.append(T('{0} net vs {1} gas', usd(m['net_per_sell']), usd(m['avg_gas_usd'])))
    if m["entry_p50"] > 0:
        key.append(T('median entry {0}', mc(m['entry_p50'])))
    if m["roi_7d"] is not None:
        key.append(T('7d {0}', pct(m['roi_7d'])))
    if m["copy_window_n"] >= 3:
        key.append(T('copy window {0}', dur(m['copy_window_s'])))
    rows.append((T('key numbers'), " · ".join(key[:4]) or T('sample too thin')))

    eng = profit_engine(m)
    if eng:
        bits = [eng[0].split(" ", 1)[-1]]
        if m["gain_top3_share"] is not None:
            bits.append(T('top 3 winners = {0}', pct(m['gain_top3_share'])))
        if m["conviction_share"] is not None:
            bits.append(T('{0} from size positions', pct(m['conviction_share'])))
        rows.append((T('profit from'), " · ".join(bits)))

    flags = [t for t in m["tag_info"] if t["sev"] in ("veto_g1", "veto_g3")] or \
            [t for t in m["tag_info"] if t["sev"] == "warn"]
    if m["honeypots"]:
        rows.append((T('top risk'),
                     T('{0} honeypots in its live book, {1} unsellable — its own screening fails too', len(m['honeypots']), usd(m['honeypot_usd']))))
    elif flags:
        rows.append((T('top risk'), f"{flags[0]['emoji']} {flags[0]['name']} · {flags[0]['meaning']}"))
    elif m["lt50_share"] >= 0.35:
        rows.append((T('top risk'),
                     T('{0} of tokens down >50% — it does not cut', pct(m['lt50_share']))))
    elif not m["security_checked"]:
        rows.append((T('top risk'),
                     T('no high-severity flags — but honeypots and the live book were not checked')))
    else:
        rows.append((T('top risk'), T('no high-severity flags')))

    return rows


def card_blocked(m, g):
    """Why the card cannot be shown, or None.

    The card's whole premise is that the reasoning is hidden, so it has nowhere to put a ⚪.
    A card with a missing tick reads as a complete verdict with one fewer reason — which is
    worse than no card, because the reader cannot see that something was not measured. So
    when the inputs are not all there, the card is withheld and the evidence layer (which
    CAN say ⚪) carries the whole answer.
    """
    if m["trades"] == 0:
        return T('no trades in the window')
    unmeasured = [k for k in ("G1", "G2", "G3", "G4") if g[k][0] is None]
    if unmeasured:
        return T('{0} not measured — the card has no way to show an unmeasured check',
                 ", ".join(T(GATE_PLAIN[k][0]) for k in unmeasured))
    if m["roi_7d"] is None:
        return T('no 7d return — the headline figure cannot be computed')
    return None


def card(m, g, wallet, chain):
    """Layer one: the decision and the action, with every 'how do you know' deferred."""
    out = []
    emoji, headline, why = verdict(m, g)
    head = f"{emoji} {headline.split(' · ')[0]}"
    flags = [t for t in m["tag_info"] if t["sev"] in ("veto_g1", "veto_g3")] or \
            [t for t in m["tag_info"] if t["sev"] == "warn"]
    if flags:
        head += f"    {flags[0]['emoji']} {flags[0]['name']}"
    out.append(head)
    out.append("")

    # ── the money, as money — but only while the record is worth quoting ──
    if g["G1"][0] is False:
        # When G1 fails the P&L is precisely what is in dispute, so quoting it as
        # "$1,000 -> $1,122" would present the disputed figure as an achieved one. The
        # reason there is no headline figure IS the headline in that case.
        put(out, "  ",
            T('Its profit figures are not trustworthy — treat the track record as unknown'))
    else:
        put(out, "  ", T('If you had followed it with {0} seven days ago',
                         usd_exact(m["story_stake"])))
        out.append(f"  {usd_exact(m['story_stake'])}  →  {usd_exact(m['story_out'])}")
        emo, label = m["form"]
        if m["pace_x"]:
            put(out, "  ", T('{0} {1} — about {2:.0f}x its own long-run pace',
                             emo, label, m["pace_x"]))
        else:
            put(out, "  ", f"{emo} {label}")
    out.append("")

    # ── the three numbers that change a decision and are not implied elsewhere ──
    # Cadence first: 16/day and 725/day are the difference between something a person can
    # do and something only a script can, and the copy window only implies it. Then the
    # return as a percentage, because that is the figure people quote to each other, and
    # the realized amount, because it is the only thing on the card that shows the SCALE
    # this wallet operates at — the size cap tells the reader their own size, not its.
    # Cadence is a fact about behaviour and survives a failed G1; the return and the amount
    # are P&L claims and do not. Printing them under "its profit figures are not
    # trustworthy" would contradict the line above them — the same defect the headline
    # figure already had, two lines lower down.
    facts = [T('{0:,.0f} trades a day', m["per_day"])]
    if g["G1"][0] is not False:
        if m["roi_7d"] is not None:
            facts.append(T('{0} over 7 days', pct(m["roi_7d"])))
        if m["realized_7d"]:
            facts.append(T('made {0} in that week', usd(m["realized_7d"])))
    put(out, "  ", " · ".join(facts), hang=2)
    out.append("")

    # ── who, in one line, with no taxonomy ──
    ident = m["twitter_name"] or (f"@{m['twitter']}" if m["twitter"] else None)
    bits = []
    if m["age_days"] is not None and m["age_days"] >= 180:
        bits.append(T('{0:.0f}-day-old wallet', m["age_days"]))
    if m["followers"] >= 10_000:
        bits.append(T('{0:,} followers', m["followers"]))
    if ident:
        put(out, "  ", ident + (("  " + " · ".join(bits)) if bits else ""))
    persona = []
    if m["gain_top3_share"] is not None and g["G1"][0] is not False:
        persona.append(T('its 3 best coins made {0} of the money', pct(m["gain_top3_share"])))
    if m["entry_p50"] > 0:
        persona.append(T('usually enters around {0}', mc(m["entry_p50"])))
    if persona:
        put(out, "  ", " · ".join(persona), hang=2)
    out.append("")

    # ── the action ──
    # A red verdict gets the verdict's own instruction, never a sizing and a copy window:
    # those are directions for FOLLOWING, and printing them under DO NOT COPY is the card
    # telling the reader to do the thing its own headline just told them not to.
    if emoji == "🔴":
        out.append(T('  WHAT TO DO'))
        put(out, "  ", why, hang=2)
    else:
        out.append(T('  HOW TO FOLLOW'))
        if m["size_cap"]:
            put(out, "  ", T('start no larger than {0}', usd_exact(m["size_cap"])))
        if m["size_ratio"]:
            if m["my_size"] > m["size_cap"]:
                put(out, "  ", T('the {0} you asked about is {1:.1f}x its own clip of {2} — '
                                 'at that size your fills are worse than the ones this '
                                 'record was built on',
                                 usd_exact(m["my_size"]), m["size_ratio"],
                                 usd_exact(m["avg_buy_usd"])), hang=2)
            else:
                put(out, "  ", T('the {0} you asked about is within that',
                                 usd_exact(m["my_size"])), hang=2)
        if m["copy_window_n"] >= 3 and m["copy_window_s"] > 0:
            put(out, "  ", T('get your order in within {0} of its buy', dur(m["copy_window_s"])))
            put(out, "  ", T('past that, let it go \u2014 its cost is lower than yours, and '
                             'entering late means buying what it is selling'), hang=2)
    out.append("")

    # ── the four outcomes, without the tests that produced them ──
    # Read the gates. These were hardcoded to "✓" in the first cut, which put
    # "✓ the record is real" on a card whose verdict was DO NOT COPY *because* that check
    # failed — the card asserting the opposite of its own headline.
    put(out, "  ", "  ".join(("✓ " if g[k][0] else "✗ ") + T(GATE_PLAIN[k][0])
                             for k in ("G1", "G2", "G3", "G4")), hang=2)
    out.append("")

    if flags:
        put(out, "  ⚠️ ", flags[0]["meaning"], hang=5)
        out.append("")

    if m["recent_buys"]:
        out.append(T('  BOUGHT IN THE LAST 24H'))
        for sym, usd_v in m["recent_buys"][:3]:
            out.append(f"  {wpad(sym[:16], 18)}{usd(usd_v)}")
        out.append("")

    if m["open_value"] and m["open_book"]:
        top = m["open_book"][0]
        # "Not a wallet that only churns" is a defence of the record, so it must not appear
        # on a card whose flag says the record may be churn. Same facts, no editorial.
        tpl = ('It is still holding {0} coins worth {1} — biggest is {2} at {3}.'
               if g["G1"][0] is False else
               'It is still holding {0} coins worth {1} — biggest is {2} at {3}. '
               'Not a wallet that only churns.')
        put(out, "  ", T(tpl, m["holdings_n"], usd(m["open_value"]),
                         top["sym"], usd(top["usd"])), hang=2)
        out.append("")
    return out


def report(wallet, chain, m, g, gaps, brief=False):
    """Two layers, in reading order.

    Layer one is the decision: verdict, the return told as money, who this is, what to do.
    Layer two is the evidence behind every one of those claims. The split exists because the
    two audiences are different and were fighting over the same screen — a newcomer needs to
    stop reading after the card, and whoever is checking the work needs every number. Putting
    the evidence second serves both; putting it first served neither.

    The bridge line at the end of layer one is not decoration. Hiding the reasoning without
    saying it exists reads as hand-waving; naming where it is makes the clean first screen a
    choice the reader can decline.
    """
    out = []
    w = wallet if len(wallet) <= 14 else f"{wallet[:6]}…{wallet[-4:]}"
    emoji, headline, why = verdict(m, g)
    BAR = "━" * 66

    blocked = card_blocked(m, g)
    if not blocked:
        out.append(BAR)
        out += card(m, g, wallet, chain)
        put(out, "  ", T('Every claim above is backed by a number. The evidence is below: '
                         'what each of the four checks actually tested, and the raw figures.'),
            hang=2)
        out.append("")
        put(out, "  ", T('Check the chips on these coins yourself before following. '
                         'All of this measures behaviour that already happened — '
                         'not a prediction, not advice.'), hang=2)
        out.append("")
        if brief:
            return "\n".join(out)
        out.append(BAR)
        out.append(T('EVIDENCE'))
        out.append("")
    elif brief:
        # Asked for the card, cannot honestly produce one. Say why rather than emitting a
        # card with a hole in it, and hand back the full report instead of nothing.
        out.append(BAR)
        put(out, T('NO CARD  '), blocked)
        out.append(BAR)
        out.append("")

    # ── verdict: the only thing on the first screen ──
    out.append(BAR)
    out.append(f"{emoji} {headline}")
    out.append(BAR)
    put(out, T('DO THIS  '), why)
    out.append("")
    out.append(
        T('{0} · {1} · window 7d (all-time from profits --period all)', w, chain)
    )
    out.append("")

    if m["trades"] == 0:
        out.append(T('NEXT'))
        for step in (
            T('Confirm this is a wallet, not a token contract — a contract queries fine and returns zeros everywhere, which looks like an answer and is not one.'),
            T('Confirm the chain: base58 → sol, 0x → bsc/base/eth.'),
            T('If it is a wallet, use gmgn-portfolio holdings to see whether it only ever received transfers or airdrops.'),
        ):
            put(out, "  • ", step)
        if gaps:
            out.append("")
            out.append(T('DATA GAPS:'))
            for gp in gaps:
                out.append(f"  ⚪ {gp}")
        return "\n".join(out)

    # ── speed read: finishes the decision without scrolling ──
    # The label column is measured, not guessed. Hardcoding 10 left the widest label
    # ("key numbers", 11 columns) unpadded, so that one row's value and every continuation
    # line under it sat a column off from the rest of the block.
    out.append(T('⚡ SPEED READ'))
    sr = speed_read(m, g, why)
    labw = max(dwidth(lab) for lab, _v in sr)
    for lab, val in sr:
        put(out, f"  {wpad(lab, labw)}  ", val, hang=labw + 4)
    out.append("")

    # ── identity ────────────────────────────────────────────────────────────────
    # Built as (label, value) rows, not a flat list of lines. The block used to mix three
    # indents — style at 10, its gloss at 13, identity and every badge at 2 — which read as
    # a wall rather than a table, and the badges took one line each.
    rows_id = []
    st, sp = style_title(m), style_speed(m)
    if st:
        head = f"{st[0]} {st[1]}"
        if sp:
            head += f" · {sp[0]} {sp[1]}" + T(" ({0})", sp[2])
        rows_id.append((T('style'), head))
        rows_id.append(("", T('{0} · cadence×P&L {1}', st[2], st[3])))

    if m["twitter_name"] or m["twitter"]:
        bits = [(f"{m['twitter_name'] or ''} @{m['twitter']}" if m["twitter"]
                 else m["twitter_name"]).strip()]
        if m["blue"]:
            bits.append(T('blue-verified'))
        if m["followers"]:
            bits.append(T('{0:,} followers', m['followers']))
        rows_id.append((T('account'), " · ".join(bits)))
        # Spell the profile out. Someone who searched this address wants to know whose
        # account it is, and a bare @handle still leaves them to go and find it.
        if m["twitter"]:
            rows_id.append(("", f"x.com/{m['twitter']}"))
    elif not (m["tags"] or m["fund_from"] or m["fund_from_address"]):
        rows_id.append((T('account'),
                        T('no X account bound and no traceable funding source — an anonymous address')))
    else:
        rows_id.append((T('account'), T('no X account bound (no public identity on GMGN)')))

    prov = [f"{t['emoji']} {t['name']}" for t in m["tag_info"] if t["sev"] == "neutral"]
    if m["age_days"] is not None:
        prov.append(T('{0:.0f}-day-old wallet', m['age_days']))
    if m["native_balance"] > 0:
        prov.append(T('{0:,.1f} {1} on hand', m["native_balance"], NATIVE.get(chain, chain.upper())))
    if m["fund_from"] or m["fund_from_address"]:
        src = m["fund_from"] or f"{m['fund_from_address'][:6]}…"
        prov.append(T('funded from {0}', src)
                    + (f" {usd(m['fund_amount'])}" if m["fund_amount"] else ""))
    if m["launchpads"]:
        mix = T(', ').join(f"{k}×{v}" for k, v in m["launchpads"])
        prov.append(T('hunts on {0}', mix))
    if m["dev_total"]:
        prov.append(T('launched {0} tokens ({1} graduated · {2})', m['dev_total'], m['dev_open'], pct(m['dev_open_ratio'])))
    elif m["created_tokens_n"]:
        prov.append(T('launched {0} tokens', m['created_tokens_n']))
    if prov:
        rows_id.append((T('provenance'), " · ".join(prov)))

    marks = archetype(m)
    if marks:
        rows_id.append((T('marks'), " · ".join(marks)))

    eng = profit_engine(m)
    if eng:
        chip, detail, meaning = eng
        rows_id.append((T('engine'), chip))
        rows_id.append(("", detail))
        rows_id.append(("", "\u2192 " + meaning, 2))

    # ── who it is: straight after the speed read, ahead of the gates ──────────────
    # A reader who searched this address wants to know WHOSE wallet it is before any
    # judgement about it. Burying the bound X account below the gates and the risk flags
    # made a newcomer scroll past four verdicts to reach the one fact they came for.
    if rows_id:
        out.append(T('👤 WHO IT IS'))
        labw = max(dwidth(r[0]) for r in rows_id)
        for row in rows_id:
            lab, val = row[0], row[1]
            extra = row[2] if len(row) > 2 else 0
            put(out, f"  {wpad(lab, labw)}  ", val, hang=labw + 4 + extra)
        out.append("")

    # ── the four gates ──
    strip = "  ".join(f"{mark(g[k][0])}{k}" for k in ("G1", "G2", "G3", "G4"))
    out.append(T('🚦 THE FOUR GATES    {0}', strip))
    for k in ("G1", "G2", "G3", "G4"):
        name = GATE_NAMES[k]
        gloss_en = GATE_GLOSS[k]
        gloss = T(" ({0})", T(gloss_en))
        out.append(f"  {mark(g[k][0])} {k} {T(name)}{gloss}")
        detail = g[k][1]
        for item in (detail if isinstance(detail, list) else [detail]):
            put(out, "     • ", item, hang=7)
    out.append("")

    # ── risk flags: binary facts, no paragraph to parse ──
    risk = []
    for t in m["tag_info"]:
        if t["sev"] in ("veto_g1", "veto_g3", "warn"):
            risk.append(f"{t['emoji']} {t['name']} · {t['meaning']}")
    if m["honeypots"]:
        syms = joinsym(x["sym"] for x in m["honeypots"])
        risk.append(T('🍯 {0} honeypot positions ({1}) · {2} unsellable', len(m['honeypots']), syms, usd(m['honeypot_usd'])))
    good = [f"{t['emoji']} {t['name']} · {t['meaning']}" for t in m["tag_info"] if t["sev"] == "good"]
    # A clean screen is reassurance, not a risk — it must not inflate the risk count.
    if not m["honeypots"] and m["security_checked"] and m.get("hp_refuted"):
        syms = joinsym(x["sym"] for x in m["hp_refuted"])
        mx = max(x["sells"] for x in m["hp_refuted"])
        good.append(T('✅ {0} honeypot flags ({1}) refuted by fill history — the busiest has {2:,} completed sells; transfer-restricted tokenised stocks, not honeypots', len(m['hp_refuted']), syms, mx))
    elif not m["honeypots"] and m["security_checked"]:
        good.append(T('✅ honeypot flag checked on {0} positions, none hit', m['security_checked']))
    if risk:
        out.append(T('🚩 RISK FLAGS ({0})', len(risk)))
        for r in risk:
            put(out, "  ", r, hang=4)
    else:
        out.append(T('✅ NO RISK FLAGS'))
    for gd in good:
        put(out, "  ", gd, hang=4)
    if risk or good:
        out.append("")


    # ── numbers panel: every row carries its own conclusion ──
    out.append(T('📊 NUMBERS (the conclusion is on the right)'))
    rows = []
    rows.append((T('P&L'),
                 T('{0} on {1} cost = {2}', usd(m['realized_7d']), usd(m['cost_7d']), pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a'),
                 roi_label(m["roi_7d"])))
    rows.append((T('form'),
                 T('1d {0} · 7d {1} · 30d {2} · all {3}', pct(m['roi_1d']) if m['roi_1d'] is not None else 'n/a', pct(m['roi_7d']) if m['roi_7d'] is not None else 'n/a', pct(m['roi_30d']) if m['roi_30d'] is not None else 'n/a', pct(m['roi_all']) if m['roi_all'] is not None else 'n/a'),
                 f"{m['form'][0]} {m['form'][1]}"))
    if m["realized_all"] is not None:
        allt = T('{0} realized', usd(m['realized_all']))
        if m["unrealized"]:
            allt += T(' · {0} on paper', usd(m['unrealized']))
        rows.append((T('all time'), allt,
                     roi_label(m["roi_all"]) if m["roi_all"] is not None else T('unknown')))
    rows.append((T('cadence'),
                 T('{0:,} trades ({1:,} buy / {2:,} sell) = {3:,.0f}/day', m['trades'], m['buy'], m['sell'], m['per_day']),
                 cadence_label(m["per_day"])))
    fr = T('{0} net per exit · {1} avg gas', usd(m['net_per_sell']), usd(m['avg_gas_usd']))
    if m["gas_drag"] is not None:
        fr += (T(' · fees {0} = {1} of profit', usd(m['fee_total']), pct(m['gas_drag']))
               if m["fee_exact"] else T(' ≈ {0} of profit (estimated)', pct(m['gas_drag'])))
    rows.append((T('friction'), fr, friction_label(m)))
    hold = T('mean {0} · median copy window {1} ({2} round trips)', dur(m['avg_hold_s']), dur(m['copy_window_s']), m['copy_window_n'])
    rows.append((T('holding'), hold,
                 T('⚠️ read the median')
                 if m["hold_conflict"] else T('mean is usable')))
    rows.append((T('entry'),
                 T('p25/p50/p75 {0}/{1}/{2} ({3} measurable)', mc(m['entry_p25']), mc(m['entry_p50']), mc(m['entry_p75']), m['entry_n']),
                 entry_label(m["entry_p50"])))
    _sz_extra = ""
    if m["size_ratio"]:
        _sz_extra = T(' · your {0} = {1:.1f}x its clip',
                      usd_exact(m["my_size"]), m["size_ratio"])
    rows.append((T('size'),
                 T('{0} per buy', usd(m['avg_buy_usd'])) + _sz_extra,
                 T('start at ≤ {0}', usd(m['size_cap']))
                 if m["size_cap"] else T('not computable')))
    # State the denominator. A bare "23.9% over 209 tokens" invites the reader to compare it
    # against the 188 sitting in the 0-200% band and conclude one of them is wrong.
    if m["dist_gap"]:
        wr_txt = T('{0} — about {1} of {2} tokens have a realized win · {3} heavy losses',
                   pct(m['winrate']), m['implied_winners'], m['token_num'], pct(m['lt50_share']))
    else:
        wr_txt = T('{0} over {1} tokens · {2} heavy losses',
                   pct(m['winrate']), m['token_num'], pct(m['lt50_share']))
    rows.append((T('win rate'), wr_txt,
                 T('cuts losses') if m["lt50_share"] < 0.35 else T('does not cut')))
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
        out.append(T("  profit concentration {0} (largest winner's share of all gains)", pct(m['pcr'])))
    out.append("")

    # ── P&L distribution ──
    b = m["buckets"]
    peak = max(b.values()) or 1
    out.append(T('📉 OUTCOME DISTRIBUTION ({0} tokens — counts tokens, not dollars)', m['token_num']))
    for lab, k in ((">500%", "gt5"), ("200–500%", "x2_5"), ("0–200%", "x0_2"),
                   ("−50–0%", "n50_0"), ("<−50%", "lt_n50")):
        n = b[k]
        out.append(f"  {lab:<10} {n:>5}  " + ("█" * max(1, int(round(30 * n / peak))) if n else ""))
    if m["dist_gap"]:
        put(out, "  ⚠️ ",
            T('The 0-200% band is not a win count. It holds {0} tokens while the win rate '
              'implies about {1} winners — the other {2} were bought and have no realized '
              'result yet, so they sit at 0% inside that band. Read the win rate for how '
              'often it wins, and this chart only for the shape of the tail.',
              m["buckets"]["x0_2"], m["implied_winners"], m["unsettled"]), hang=5)
    out.append("")

    # ── what it is doing now ──
    pe, pl = m["posture"]
    out.append(T('🔄 WHAT IT IS DOING NOW'))
    out.append(T('  {0} {1} · 24h bought {2} / sold {3}', pe, pl, usd(m['buy_usd_24h']), usd(m['sell_usd_24h'])))
    if m["idle_s"] is not None:
        put(out, "  ", (T('⚠️ last trade {0} ago — every figure here describes a wallet that '
                          'has since gone quiet', dur(m["idle_s"])) if m["stale"]
                        else T('last trade {0} ago', dur(m["idle_s"]))), hang=2)
    if m["recent_buys"]:
        put(out, T('  bought in 24h: '),
            ", ".join(f"{sym} {usd(v)}" for sym, v in m["recent_buys"]))
    if m["open_book"]:
        out.append(T('  {0} positions · {1} total', m['holdings_n'], usd(m['open_value'])))
        hp_syms = {x["sym"] for x in m["honeypots"]}
        for bk in m["open_book"]:
            tag = " 🍯" if bk["sym"] in hp_syms else ""
            out.append(f"    {wpad(bk['sym'] + tag, 14)}{usd(bk['usd']):>10}  {pct(bk['chg'], 0):>8}  "
                       + T('cost {0} · {1} sells', usd(bk['cost']), bk['sells']))
    else:
        out.append(T('  live book: unavailable (see data gaps)'))
    out.append("")

    # ── what to do next ──
    out.append(T('✅ WHAT TO DO NEXT'))
    for a in actions(m, g):
        put(out, "  • ", a)
    out.append("")

    cap = T(' (hit page cap — busiest slice only)') if m["hit_limit"] else ""
    put(out, "", T('sample  {0:,} activity rows / {1} tokens · spans {2:.1f}h{3}', m['sampled'], m['distinct_tokens_sampled'], m['span_h'], cap))
    if gaps:
        out.append(T('DATA GAPS (unevaluated ≠ passed):'))
        for gp in gaps:
            put(out, "  ⚪ ", gp)
    out.append("")
    put(out, "", T('Everything above measures behaviour that already happened. Not a prediction, not advice.'))
    return "\n".join(out)


def actions(m, g):
    a = []
    p = {k: v[0] for k, v in g.items()}
    if m["trades"] == 0:
        return [
            T('Confirm this is a wallet, not a token contract. If it is a wallet, wait for real trades.')
        ]
    if m["recent_buys"]:
        syms = ", ".join(s for s, _v in m["recent_buys"][:3])
        a.append(
            T('It bought {0} in the last 24h — run gmgn-token / gmgn-holder-analysis on those before following it in.', syms)
        )
    if p["G3"] is False:
        a.append(
            T('Do not mirror it. Treat it as a signal source: note what and at what mcap, then enter on your own terms.')
        )
    elif p["G3"] is True and m["size_cap"]:
        a.append(
            T('Start at ≤ {0} (it averages {1} per buy; above its own size your slippage is worse than its). Quote through gmgn-swap before sending.', usd(m['size_cap']), usd(m['avg_buy_usd']))
        )
    if p["G4"] is False:
        a.append(
            T('Set your own stop — it does not cut, and riding it to the end means riding it to zero.')
        )
    if m["copy_window_s"] > 0:
        a.append(
            T('If you copy it, your order must land within {0} of its buy — otherwise skip the trade.', dur(m['copy_window_s']))
        )
    if m["is_dev"]:
        a.append(
            T('This is a launcher. Do not score its trading — check its launch survival and security record (gmgn-wallet-score, Dev angle).')
        )
    if m["form"][1] in (T('cooling off'), T('broken down')):
        a.append(
            T('Its money is historical. Re-run this in 7 days to see whether form recovers or keeps sliding.')
        )
    a.append(
        T('For 0-100 scores and a latency/slippage backtest, use gmgn-wallet-score.')
    )
    return a


# ─────────────────────────── entry ───────────────────────────


def main(argv):
    args = [a for a in argv[1:]]
    latency_s, my_size, fixture, brief = 3.0, None, None, False
    rest = []
    k = 0
    while k < len(args):
        if args[k] == "--latency" and k + 1 < len(args):
            latency_s = f(args[k + 1], 3.0)
            k += 2
        elif args[k] == "--size" and k + 1 < len(args):
            my_size = f(args[k + 1])
            k += 2
        elif args[k] == "--brief":
            brief = True
            k += 1
        elif args[k] == "--fixture" and k + 1 < len(args):
            fixture = args[k + 1]
            k += 2
        else:
            rest.append(args[k])
            k += 1

    lang = next((x for x in rest if x in ("zh", "en")), "zh")
    load_lang(lang)
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
                T('Data pull failed, no verdict possible: {0}\nCheck `gmgn-cli config --check` first; on 429 wait for the stated reset; on 401/403 with valid credentials check IPv6 (gmgn-cli is IPv4 only).', e)
            )
            return 1

    m = compute(d, latency_s, my_size)
    g = gates(m)
    print(report(wallet, chain, m, g, gaps, brief))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
