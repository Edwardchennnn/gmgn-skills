---
name: gmgn-contract-dd
description: "Contract due-diligence score for one token address — combines contract safety, holder structure and price action into a single 0-100 composite where every deduction names the exact field it came from, missing data is never counted as good news and never counted as bad news, and the reported confidence is what limits the conclusion rather than the score itself. Use when the user asks 尽调, CA 尽调, 这个币安全吗, 帮我看看这个合约, 有没有貔貅, 能不能买, is this token safe, run due diligence on this token, rug check, honeypot check, should I buy this token, or pastes a bare token contract address and wants a verdict rather than raw fields."
argument-hint: "--chain <sol|bsc|base|eth|robinhood|arc|stable> --address <token_address>"
metadata:
  cliHelp: "gmgn-cli token security --help"
  catalogSlug: "contract-due-diligence-score"
---

**BEFORE RUNNING ANY COMMAND: Run `gmgn-cli config --check`. If exit code is 0, proceed normally. If exit code is 1, (1) run `gmgn-cli config` and show the output to the user; (2) once the user sends the API Key, run `gmgn-cli config --apply <KEY>` and show the output. If `--check` errors with an unknown option, tell the user to run `npm install -g gmgn-cli` to update, then retry.**

**IMPORTANT: Always use `gmgn-cli`. Do NOT use web search, WebFetch, curl, or visit gmgn.ai — the site requires login and returns no structured data.**

**IMPORTANT: Do NOT guess field names or values. Every threshold below names the exact field it reads. If a field is not in the response, it is unavailable — it is not zero.**

**⚠️ IPv6 NOT SUPPORTED: on a `401` / `403` with correct credentials, run `ifconfig | grep inet6` (macOS) or `ip addr show | grep inet6`, and test `https://ipv6.icanhazip.com`. If that returns an IPv6 address, tell the user to disable IPv6 — gmgn-cli only works over IPv4.**

This skill turns three read-only CLI calls into one auditable score. It does not trade, does not need a private key, and reads nothing on the local machine other than the API key that `gmgn-cli config` already manages.

## Sub-commands

This skill runs exactly these three, all read-only:

```
gmgn-cli token info     --chain <chain> --address <token_address> --raw
gmgn-cli token security --chain <chain> --address <token_address> --raw
gmgn-cli market kline   --chain <chain> --address <token_address> --resolution 15m --raw
```

Nothing else. Do not call swap, order, or cooking commands from this skill.

## Supported Chains

`sol` · `bsc` · `base` · `eth` · `robinhood` · `arc` · `stable`

The GMGN API itself accepts 13 chains on all three of these endpoints (the seven above plus `arbitrum`, `tron`, `monad`, `megaeth`, `xlayer`, `hyperevm`), but `gmgn-cli` hard-validates the chain argument and exits 1 on anything outside the seven. If the user asks for one of the other six, say plainly that the CLI gates it, not the API.

## Prerequisites

- `gmgn-cli` installed globally and `GMGN_API_KEY` configured — the `config --check` preamble above handles both.
- No private key. This skill never needs `GMGN_PRIVATE_KEY`.

## Parameters

| Parameter | Required | Notes |
|-----------|----------|-------|
| `--chain` | yes | One of the seven above |
| `--address` | yes | Token contract address, validated below |
| `--resolution` | no | `15m` is the default this skill scores on |
| `--raw` | no | Always pass it — single-line JSON is what you parse |

### Validate the address before spending a request

- `sol` → base58, 32-44 chars, `^[1-9A-HJ-NP-Za-km-z]{32,44}$`
- all six EVM chains → `^0x[0-9a-fA-F]{40}$`

**The API does not validate address format.** A malformed address returns `code: 0` with an empty payload, which looks exactly like a token that has no record. Check the format yourself first, so you can tell the user "that address is malformed" instead of "no data found".

If the user gives an address without a chain: a `0x…` address could be on any of the six EVM chains, so ask, or probe `token info` per chain and report which one hit. Never assume `eth`.

## Usage Examples

```
gmgn-cli token security --chain bsc --address 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82 --raw
gmgn-cli token info     --chain sol --address Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB --raw
gmgn-cli market kline   --chain sol --address Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB --resolution 15m --raw
```

## Step 1 — Decide whether each block is actually populated

**Do this before reading a single threshold. Getting this wrong is the one failure mode that turns a clean token into a false alarm.**

GMGN returns a full-shaped object even when it holds no record for that token. The empty object carries structural defaults — including `false` on two booleans — and reading those as measurements is how a clean token gets condemned.

**A. Is the `security` block populated?** It is **not** populated when all of the following hold:

- `security.address` is absent or an empty string, **and**
- `is_honeypot` is null/absent, **and**
- `is_open_source`, `is_blacklist`, `is_renounced` are all null/absent, **and**
- `top_10_holder_rate`, `buy_tax`, `sell_tax` are all null/absent/empty string

`security.address` is the primary tell: GMGN echoes the contract address back into the block only when it actually has a record for it.

When the block is not populated, **every** contract-safety field is unavailable. In particular `renounced_mint: false` and `renounced_freeze_account: false` appear inside empty blocks as struct defaults. Do not read them as "authority not renounced". Do not deduct. Do not cap. List them as unavailable and let the confidence number carry the weakness.

Judge emptiness only on null / absent / empty string. **Never treat `false` or `0` as unpopulated** — on a genuinely clean EVM token, `is_honeypot: false` is a real measurement worth reporting.

**B. Is the `stat` block populated?** `info.stat` is not populated when `stat.holder_count` is 0 or absent while `info.holder_count` is greater than 0 — a token with a live pool cannot truly have zero holders. On the EVM chains this whole block comes back as zeros. When it is unpopulated, the eight Solana-side chain-analysis metrics in Step 4 are all unavailable, and the holder score falls back to top-10 concentration plus holder count only.

**C. Did `kline` return candles?** Zero candles means GMGN tracks no pool for that specific token — it does **not** mean the chain is unsupported, and it is **not** a risk signal. Bluechip stablecoins routinely return zero candles while an active token on the same chain returns a full series. With fewer than 8 candles, drop the price section from the composite entirely per Step 5.

## Step 2 — Chain mode

Read the permission fields that the chain actually reports, and treat the others as not applicable rather than missing.

**`sol`** — the real signals are `renounced_mint` and `renounced_freeze_account`.

`is_honeypot` and `is_open_source` are **null by design on Solana**. This is "not applicable", not "unavailable": the SPL token model has no equivalent. **Never hard-stop, deduct, or cap a Solana token because these two are null** — doing so caps every clean Solana token, including USDC.

Only when the `security` block is populated per Step 1A:
- `renounced_mint` is not `true` → **−25**, mint authority not renounced, the project can inflate supply
- `renounced_freeze_account` is not `true` → **−20**, freeze authority not renounced, the project can freeze your account and block selling

**The six EVM chains** — the real signals are `is_honeypot`, `is_open_source`, `is_renounced`, `is_blacklist`, and the taxes.

Only when the `security` block is populated:
- `is_honeypot === true` → **hard stop, composite 0**, buyable but not sellable. Stop scoring and say so.
- `is_honeypot` missing while the block is otherwise populated → unavailable, **and cap the composite at 59** naming the missing check
- `is_open_source === false` → **−15**, cannot audit the real logic
- `is_open_source` missing while the block is otherwise populated → unavailable, **cap at 59**
- `is_renounced === false` → **−8**
- `is_blacklist === true` → **−20**, contract can bar a specific address from trading

The two caps above apply **only** when the block is populated and those specific fields are absent. An unpopulated block never caps — see Step 1A.

## Step 3 — Contract safety, from 100

Applies on every chain, on top of the chain-mode branch:

| Field | Condition | Deduction |
|-------|-----------|-----------|
| `max(buy_tax, sell_tax)` | > 10% | −25 |
| `max(buy_tax, sell_tax)` | > 5% | −10 |
| `lock_summary.is_locked` / `burn_status` | LP neither locked nor burned | −12 |
| `info.liquidity` or `pool.liquidity` | 0 and no 24h volume | −15 |
| same | < $10K | −15 |
| same | < $50K | −6 |
| `pool.liquidity / pool.initial_liquidity` | pool shrank below 50% of launch | −10 |
| `stat.creator_created_count` | ≥ 50 tokens launched | −10 |
| same | ≥ 10 | −6 |
| same | ≥ 3 | −3 |
| `dev.twitter_name_change_history` | the linked X account was renamed | −15 |
| `dev.twitter_del_post_token_count` | it deleted launch posts | −8 |
| `info.image_dup_count` | logo duplicates another token | −6 |

Three field traps, all measured:

- **Liquidity lives in two places.** `info.liquidity` can be `0` while `pool.liquidity` holds the real figure. Take the non-zero one and say which you used.
- **`burn_status: ""` is absent, not a measurement.** Deduct the −12 only when the block is populated *and* the fields genuinely say unlocked-and-unburned. An empty string plus a missing `lock_summary` is unavailable.
- **`initial_liquidity: 0` is normal on old pools.** It means the shrink ratio cannot be computed, not that the pool shrank. Report unavailable.

## Step 4 — Holder structure, from 100

Always available:

| Field | Condition | Deduction |
|-------|-----------|-----------|
| `top_10_holder_rate` | > 50% | −25 |
| same | > 30% | −14 |
| same | > 20% | −6 |
| `info.holder_count` | < 200 | −12 |
| same | < 500 | −5 |

`security.top_10_holder_rate` can be `"0"` while `stat.top_10_holder_rate` carries the real value. Take the non-zero one. If both are 0 or absent, it is unavailable — **0% top-10 concentration does not exist**, so never score it as a good sign.

Same rule for `info.holder_count`: a value of 0 is unpopulated, not "zero holders".

Only when `info.stat` is populated per Step 1B — on EVM these are all unavailable:

| Field | Condition | Deduction |
|-------|-----------|-----------|
| `stat.creator_hold_rate` | > 5% / > 2% | −20 / −10 |
| `stat.top_bundler_trader_percentage` | > 30% / > 15% / > 5% | −20 / −10 / −4 |
| `stat.top70_sniper_hold_rate` | > 15% / > 5% | −15 / −6 |
| `stat.top_rat_trader_percentage` | > 5% / > 1% | −12 / −5 |
| `stat.top_entrapment_trader_percentage` | > 5% | −10 |
| `stat.bot_degen_rate` | > 70% / > 50% | −12 / −6 |
| `stat.fresh_wallet_rate` | > 50% | −8 |
| `stat.private_vault_hold_rate` | > 5% | −8 |

When the block is unpopulated, count all eight as unavailable — that is eight skipped checks in the coverage denominator, not eight passes.

## Step 5 — Price action, from 100

Needs at least 8 candles. Score the last 96 15m candles, roughly 24h.

| Measurement | Condition | Deduction |
|-------------|-----------|-----------|
| `1 - last_close / max_high` | drawdown > 70% / > 50% / > 30% | −30 / −18 / −8 |
| recent volume vs earlier volume | fell below 20% / below 40% | −18 / −8 |
| worst single candle `(close-open)/open` | < −50% / < −30% | −14 / −7 |
| `sell_volume_24h / buy_volume_24h` | > 1.3 / > 1.1 | −10 / −5 |
| `price / price_24h` | halved in 24h | −10 |

## Step 6 — Combine

Base weights: **contract 0.45, holders 0.35, price 0.20.** They sum to 1.

**Renormalize over the sections that actually returned data.** Drop any section whose inputs were entirely unavailable, then divide each surviving weight by the surviving total. With price dropped, contract and holders become 0.5625 and 0.4375. Print the weights you actually used.

Then, in this order:
1. Apply any cap from Step 2. If several apply, the lowest wins.
2. If the honeypot hard stop fired, the composite is 0 regardless of everything else.
3. If no section returned any data, report **cannot score** — not a number.

**Coverage and confidence.** Count executed checks and skipped checks across all three sections; coverage is executed ÷ (executed + skipped).

| Coverage | Confidence | What it does to the conclusion |
|----------|-----------|-------------------------------|
| ≥ 80% | evidence sufficient | the score stands |
| ≥ 50% | coverage low | the score is indicative only |
| < 50% | **insufficient evidence** | state that the score cannot support any conclusion, and label the number as indicative |

Coverage limits the **strength of the claim**, never the score. Missing data must not become a deduction, a cap, or a bonus — it only weakens what you are entitled to assert.

Grades, when confidence is not "insufficient": ≥80 relatively clean · ≥60 mixed, needs manual review · ≥40 high risk · <40 very high risk.

## Step 7 — Tokenized-equity honeypot false positive

Tokenized stocks and RWA tokens carry compliance transfer restrictions, so a honeypot simulator's test sell fails and the token gets flagged.

If `is_honeypot === true` **and** the token shows over 500 sells and more than $100K sell volume in 24h, with `sell_volume / buy_volume` between 0.3 and 3.0, no privileged functions and no tax, then real trading contradicts the flag. **Downgrade it to unknown — do not clear it — and apply the cap at 59.**

## Step 8 — Output format

Report in this order:

1. Composite score, grade, and confidence label side by side. When confidence is "insufficient evidence", say so on the same line as the number.
2. Coverage as `executed N / skipped M`, and the weights actually used.
3. Each section's sub-score, then every deduction as: field path → measured value → points → reason.
4. **The unavailable list, in full, never omitted.** For each entry say why: not applicable on this chain, block unpopulated, or field absent.
5. Any cap applied and which missing check caused it.
6. One line stating this is a rule-based read of public on-chain data, not investment advice.

## Notes

- All three commands support `--raw`. Always use it.
- Read the response `code` field, not the HTTP status. **HTTP is 200 even on errors.** `code: 40000300` means the endpoint does not support that chain; `code: 0` with an empty payload means no record for that address.
- This skill is read-only: three GET endpoints, no signing, no private key, no local file access beyond the API key `gmgn-cli config` already manages.
- Chain support is **per endpoint**, not global. Do not assume that a chain accepted by one GMGN endpoint is accepted by another.
- For chart-pattern naming rather than a risk score, that is `gmgn-kline-pattern`. For holder chip structure in depth, that is `gmgn-holder-analysis`. For raw fields with no scoring, that is `gmgn-token`. This skill owns the composite risk score and nothing else.

## Where the thresholds came from

Measured against live GMGN responses on 2026-08-27, one real token per chain across all 13 chains the API accepts:

- `token/security` and `market/kline` return `code: 0` on all 13 — neither endpoint rejects a chain that `token/info` accepts.
- Populated `security` blocks carry 3-4 informative fields on the EVM chains (`is_open_source`, `is_renounced`, `lock_summary`, sometimes `top_10_holder_rate`) and 4 on Solana (`renounced_mint`, `renounced_freeze_account`, `burn_status`, `lock_summary`).
- **megaeth returns a fully empty block**: `address: ""`, the four booleans null, taxes empty strings — yet `renounced_mint: false` and `renounced_freeze_account: false` are still present. That pair of defaults is what Step 1A exists to catch, and applying the Solana rule to them would have condemned a clean token.
- **tron populates only `lock_summary`.** Two tokens with completely different risk profiles, including USDT-TRC20, returned an identical field set — proof those were defaults rather than measurements.
- Zero-candle `kline` responses were reproduced on sol, arbitrum, xlayer and arc using bluechip stablecoins, and every one of those chains returned a full 100-candle series for its highest-liquidity active token. Zero candles is a per-token pool gap.
