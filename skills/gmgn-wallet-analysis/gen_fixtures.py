#!/usr/bin/env python3
"""
Build the synthetic wallets analyze.py is verified against.

Each fixture is deliberately engineered to fail exactly one gate (or none), so a
regression that makes a gate fire on everything — or on nothing — shows up immediately.
Every numeric value is emitted as a JSON *string*, matching the real API.

    python3 gen_fixtures.py && python3 analyze.py --fixture fixtures/grinder.json zh
"""

import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures")
NOW = 1_760_000_000  # fixed so reports are reproducible


def s(v):
    return str(v)


def stats(buy, sell, realized, cost, roi, token_num, winrate, hold_s, buckets, common=None):
    return {
        "buy": s(buy),
        "sell": s(sell),
        "realized_profit": s(realized),
        "bought_cost": s(cost),
        "realized_profit_pnl": s(roi),
        "pnl_stat": {
            "token_num": s(token_num),
            "winrate": s(winrate),
            "avg_holding_period": s(hold_s),
            "pnl_gt_5x_num": s(buckets[0]),
            "pnl_2x_5x_num": s(buckets[1]),
            "pnl_0x_2x_num": s(buckets[2]),
            "pnl_nd5_0x_num": s(buckets[3]),
            "pnl_lt_nd5_num": s(buckets[4]),
        },
        "common": common or {},
    }


def profits(realized, cost, total_realized, total_cost, unrealized=0):
    return {
        "realized_profit": s(realized),
        "realized_profit_cost": s(cost),
        "total_realized_profit": s(total_realized),
        "total_realized_profit_cost": s(total_cost),
        "unrealized_profit": s(unrealized),
    }


def trades(n_tokens, buys_per, sells_per, mcap, buy_usd, hold_s, gap_s, gas=0.3, supply=1e9,
           dump=False, start=NOW):
    """Emit interleaved buy/sell rows for n_tokens, newest last."""
    rows, t = [], start - n_tokens * gap_s
    for k in range(n_tokens):
        addr = f"TOKEN{k:03d}"
        sym = f"MEME{k}"
        # deterministic spread so the p25/p50/p75 entry band is not a single value
        px = mcap * (0.45 + (k % 9) * 0.16) / supply
        for b in range(buys_per):
            rows.append({
                "event_type": "buy", "timestamp": s(int(t + b * 30)),
                "price_usd": s(px), "cost_usd": s(buy_usd / buys_per),
                "gas_usd": s(gas),
                "token": {"address": addr, "symbol": sym, "total_supply": s(supply)},
            })
        sold = buy_usd * 1.4
        n_sell = 1 if dump else sells_per
        for x in range(n_sell):
            rows.append({
                "event_type": "sell", "timestamp": s(int(t + hold_s + x * 60)),
                "price_usd": s(px * 1.4), "cost_usd": s(sold / n_sell),
                "gas_usd": s(gas),
                "token": {"address": addr, "symbol": sym, "total_supply": s(supply)},
            })
        t += gap_s
    return rows


HONEYPOTS = {"QQQB", "SPYB", "GOOGLB"}


def holds(spec, launchpad="flap"):
    """spec: (symbol, usd_value, cost, total_profit, pnl_ratio, sell_count).

    Mirrors the REAL holdings schema confirmed against the live API: rows come back under
    `list`, costs are `accu_cost`, the P&L ratio is `total_profit_pnl`, sell counts are
    `history_total_sells`, and `token.is_honeypot` / `token.launchpad_platform` ship inline.
    """
    return [
        {
            "token": {"symbol": sym, "token_address": f"H{k}",
                      "is_honeypot": "true" if sym in HONEYPOTS else "false",
                      "launchpad_platform": launchpad},
            "usd_value": s(usd), "accu_cost": s(cost), "total_profit": s(tp),
            "total_profit_pnl": s(pc), "history_total_sells": s(sells),
        }
        for k, (sym, usd, cost, tp, pc, sells) in enumerate(spec)
    ]


FIXTURES = {}

# ── 1. grinder — the wallet you actually want: all four gates pass ──
FIXTURES["grinder"] = {
    "_wallet": "GrinderWallet1111111111111111111111111111",
    "stats_7d": stats(
        96, 88, 14_200, 41_000, 0.346, 34, 0.56, 9_400, (2, 6, 11, 10, 5),
        common={"created_at": s(NOW - 400 * 86400), "created_token_count": "0",
                "tags": ["smart_money"], "fund_from": "Binance",
                "fund_amount": "12000", "follow_count": "318"},
    ),
    "stats_30d": stats(380, 351, 52_000, 168_000, 0.31, 121, 0.54, 10_100, (7, 19, 41, 35, 19)),
    "profits_1d": profits(2_100, 6_400, 0, 0),
    "profits_all": profits(0, 0, 460_000, 1_520_000, 38_000),
    "activity": trades(22, 2, 3, mcap=740_000, buy_usd=1_800, hold_s=7_200, gap_s=5_400),
    "holdings": holds([
        ("PEPE2", 9_400, 6_000, 3_400, 0.56, 2),
        ("WIF", 6_100, 5_200, 2_900, 0.55, 1),
        ("MOG", 2_400, 1_500, 2_600, 1.73, 3),
        ("SPX", 4_200, 3_000, 2_100, 0.70, 2),
        ("GIGA", 1_800, 1_200, 1_400, 1.16, 1),
        ("BONKX", 3_300, 4_000, -700, -0.17, 0),
        ("TURBO", 900, 2_000, -1_100, -0.55, 1),
        ("SLERF", 400, 1_100, -700, -0.63, 1),
        ("BOME", 250, 800, -550, -0.68, 2),
    ]),
}

# ── 2. sniper-bot — real edge, unreachable: G3 fails ──
FIXTURES["sniper-bot"] = {
    "_wallet": "SniperBot22222222222222222222222222222222",
    "stats_7d": stats(
        2_400, 2_380, 61_000, 180_000, 0.34, 410, 0.48, 22, (14, 51, 160, 130, 55),
        common={"created_at": s(NOW - 90 * 86400), "created_token_count": "0",
                "fund_from": "Unknown", "tags": []},
    ),
    "stats_30d": stats(9_900, 9_800, 240_000, 720_000, 0.33, 1_600, 0.47, 25, (60, 210, 640, 500, 190)),
    "profits_1d": profits(8_600, 25_000, 0, 0),
    "profits_all": profits(0, 0, 1_900_000, 5_600_000, 4_000),
    # 4-second round trips at $18k entry mcap: you are its exit liquidity
    "activity": trades(60, 1, 1, mcap=18_000, buy_usd=240, hold_s=4, gap_s=120, gas=2.4, dump=True),
    "holdings": [],
    "_gaps": ["holdings unavailable (needs GMGN_PRIVATE_KEY / critical auth) — fixture"],
}

# ── 3. lucky-one-coin — one token carried it: G1 fails via bucket inference ──
FIXTURES["lucky-one-coin"] = {
    "_wallet": "LuckyOneCoin333333333333333333333333333",
    "stats_7d": stats(
        26, 19, 88_000, 24_000, 3.66, 14, 0.29, 68_000, (1, 0, 3, 4, 6),
        common={"created_at": s(NOW - 210 * 86400), "created_token_count": "0"},
    ),
    "stats_30d": stats(70, 55, 91_000, 60_000, 1.51, 31, 0.31, 71_000, (1, 1, 8, 10, 11)),
    "profits_1d": profits(-400, 3_100, 0, 0),
    "profits_all": profits(0, 0, 96_000, 140_000, -2_100),
    "activity": trades(11, 1, 2, mcap=420_000, buy_usd=2_200, hold_s=68_000, gap_s=43_200),
    "holdings": [],
    "_gaps": ["holdings unavailable — fixture (forces bucket-inference path)"],
}

# ── 4. cooled-star — great all-time, dead this week: G2 fails ──
FIXTURES["cooled-star"] = {
    "_wallet": "CooledStar44444444444444444444444444444",
    "stats_7d": stats(
        44, 51, -12_400, 46_000, -0.27, 21, 0.33, 15_000, (0, 1, 5, 9, 6),
        common={"created_at": s(NOW - 620 * 86400), "created_token_count": "0",
                "tags": ["smart_money"], "follow_count": "4210"},
    ),
    "stats_30d": stats(190, 205, -9_000, 210_000, -0.04, 88, 0.41, 16_400, (2, 7, 28, 30, 21)),
    "profits_1d": profits(-3_300, 9_000, 0, 0),
    "profits_all": profits(0, 0, 2_400_000, 1_900_000, -41_000),
    "activity": trades(18, 2, 2, mcap=1_100_000, buy_usd=2_600, hold_s=15_000, gap_s=9_000),
    "holdings": holds([
        ("OLDMEME", 12_000, 40_000, -28_000, -0.70, 3),
        ("LATEBAG", 4_100, 9_000, -4_900, -0.54, 1),
    ]),
}

# ── 5. dev — launcher marking its own homework: G1 fails ──
FIXTURES["dev-launcher"] = {
    "_wallet": "DevLauncher5555555555555555555555555555",
    "stats_7d": stats(
        60, 42, 31_000, 8_000, 3.87, 9, 0.78, 3_400, (3, 2, 2, 1, 1),
        common={"created_at": s(NOW - 40 * 86400), "created_token_count": "37",
                "fund_from_address": "0xfeed0000000000000000000000000000000000aa"},
    ),
    "stats_30d": stats(210, 160, 96_000, 30_000, 3.2, 30, 0.74, 3_600, (9, 6, 7, 5, 3)),
    "profits_1d": profits(4_400, 1_200, 0, 0),
    "profits_all": profits(0, 0, 310_000, 96_000, 1_400),
    "activity": trades(14, 3, 2, mcap=48_000, buy_usd=600, hold_s=3_400, gap_s=7_200, dump=True),
    "holdings": holds([("MYCOIN", 2_100, 400, 1_700, 4.2, 5)]),
    "created_tokens": {
        "open_count": "5", "inner_count": "32", "open_ratio": "0.135",
        "creator_ath_info": {"ath_mc": "2400000", "token_symbol": "MYCOIN"},
    },
}

# ── 6. no-cut — picks fine, never sells a loser: G4 fails ──
FIXTURES["no-cut"] = {
    "_wallet": "NoCutBagholder66666666666666666666666666",
    "stats_7d": stats(
        58, 21, 6_400, 30_000, 0.21, 26, 0.42, 260_000, (2, 3, 6, 3, 12),
        common={"created_at": s(NOW - 300 * 86400), "created_token_count": "0"},
    ),
    "stats_30d": stats(190, 70, 18_000, 110_000, 0.16, 74, 0.40, 280_000, (5, 9, 18, 12, 30)),
    "profits_1d": profits(500, 3_000, 0, 0),
    "profits_all": profits(0, 0, 210_000, 780_000, -96_000),
    "activity": trades(16, 2, 1, mcap=560_000, buy_usd=1_900, hold_s=260_000, gap_s=21_600),
    "holdings": holds([
        ("HOPE", 320, 8_000, -7_680, -0.96, 0),
        ("COPE", 210, 6_500, -6_290, -0.97, 0),
        ("ROPE", 90, 5_000, -4_910, -0.98, 0),
        ("NGMI", 60, 4_200, -4_140, -0.98, 0),
        ("WIN1", 14_000, 4_000, 6_000, 1.5, 2),
        ("WIN2", 7_000, 3_000, 4_800, 1.6, 1),
        ("WIN3", 5_200, 2_400, 3_900, 1.62, 2),
        ("WIN4", 2_100, 1_500, 1_700, 1.13, 1),
        ("MEH", 800, 1_000, -200, -0.20, 1),
    ]),
}

# ── 8. wash-trader KOL — modelled on a real BSC wallet: high absolute profit, a
#       wash_trader tag, 1k+ trades/day, gas eating the per-trade net, honeypots in the book.
#       Exists to prove the tag veto outranks a headline +$433K. ──
FIXTURES["wash-trader-kol"] = {
    "_wallet": "0xbf004bff64725914ee36d03b87d6965b0ced4903",
    "_chain": "bsc",
    "stats_7d": stats(
        3_895, 3_824, 101_130, 831_420, 0.1216, 467, 0.5519, 343_034, (1, 4, 315, 142, 5),
        common={
            "created_at": s(NOW - 470 * 86400), "created_token_count": "0",
            "tags": ["kol", "wash_trader", "top_followed", "top_renamed", "gmgn"],
            "twitter_username": "aa_AFeng", "twitter_name": "阿峰_Afeng",
            "is_blue_verified": "true", "followers_count": "47999",
            "follow_count": "1820", "fund_from": "Binance", "fund_amount": "50000",
        },
    ),
    "stats_30d": stats(16_685, 16_387, 433_415, 3_563_228, 0.1046, 2_000, 0.5519, 343_034,
                       (1, 18, 1_350, 610, 21)),
    "profits_1d": profits(9_800, 96_000, 0, 0),
    "profits_all": profits(0, 0, 1_120_000, 9_800_000, -4_100),
    # seconds-level flips on freshly launched sub-$100k tokens, $4 gas on ~$340 of size
    "activity": trades(40, 1, 1, mcap=62_000, buy_usd=340, hold_s=95, gap_s=180,
                       gas=4.05, dump=True),
    "holdings": holds([
        ("WBNB", 3_745, 3_745, 0, 0.0, 0),
        ("QQQB", 113, 628, -514, -0.82, 1),
        ("SPYB", 37, 2_657, -2_620, -0.98, 1),
        ("USD1", 221, 228, -7, -0.03, 1),
        ("MUB", 77, 80, -2, -0.03, 1),
        ("GOOGLB", 0.82, 7, -6, -0.89, 1),
        ("BNC", 210, 340, -130, -0.38, 2),
        ("EASY", 95, 340, -245, -0.72, 1),
        ("NAKA", 610, 340, 270, 0.79, 2),
    ]),
}

# ── 7. empty — a token contract pasted as a wallet, or a fresh address ──
FIXTURES["empty"] = {
    "_wallet": "EmptyOrTokenContract7777777777777777777",
    "stats_7d": stats(0, 0, 0, 0, 0, 0, 0, 0, (0, 0, 0, 0, 0), common={"created_token_count": "0"}),
    "stats_30d": stats(0, 0, 0, 0, 0, 0, 0, 0, (0, 0, 0, 0, 0)),
    "profits_1d": profits(0, 0, 0, 0),
    "profits_all": profits(0, 0, 0, 0),
    "activity": [],
    "holdings": [],
}

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, data in FIXTURES.items():
        data.setdefault("_chain", "sol")
        with open(os.path.join(OUT, f"{name}.json"), "w") as fh:
            json.dump(data, fh, indent=1)
        print(f"wrote fixtures/{name}.json")
