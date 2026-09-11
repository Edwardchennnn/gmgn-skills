# Required interfaces — configuration contract

This skill is deliberately written to be model- and provider-agnostic. Nothing in `SKILL.md` should be read as tied to a specific AI vendor or a specific data vendor. This file is the concrete contract an integrator fills in before the skill can run in a new environment (e.g. before adding it to a different agent's skill set).

## 1. Agent / model runtime

| Item | Requirement |
|---|---|
| Tool use | Must support multi-step, agentic tool calling (the model decides what to call next based on prior results — not a single fixed pipeline). |
| Reasoning | Must be able to read unstructured natural-language text (social posts, web pages) and judge whether it actually supports a specific claim. |
| Turn budget | A single card can take on the order of 10-20 tool calls (on-chain lookup, several search queries, opening several links). The runtime must allow that many steps for one request without truncating. |
| Validated on | Claude Sonnet 5, with a harness providing the two interfaces below. |
| Not yet validated on | Any other model or harness. Do not assume parity — run a small validation pass (a handful of real tokens, output compared against known-good cards) before trusting a new model/runtime combination in production. |

No API key or secret is required for the model itself beyond whatever the hosting platform already requires — this section exists to document *capability* requirements, not credentials.

### Validation pass — run this before trusting a new model/runtime

This is a short, concrete check, not a formality. Pick 5-8 real tokens (mix of thin/quiet ones and ones with a genuine story) and for each, check the model's output against these specific failure modes rather than just reading it for general quality:

1. **Fabrication under thin evidence.** Give it a token with almost no real social activity. Does it write a plain neutral statement, or does it manufacture a plausible-sounding "finding"?
2. **Skipped verification.** Spot-check 2-3 specific claims in the output by opening the source yourself. Does every claim actually trace to something the model could have opened, or does at least one look like it was written from general knowledge / a plausible guess?
3. **Misattribution across similarly-named projects.** Deliberately test a token whose name is close to something else well-known. Does the output ever describe the wrong project?
4. **Rule adherence at the end of a long run.** In whichever case took the most tool calls, check whether the later hard rules (no platform-naming, the disclaimer, the score caveat) are still followed as strictly as in a short, easy case.
5. **Register.** For non-English output, does it read as a finished, professional deliverable, or does it drift into stiff/translated-sounding phrasing or slang under different tokens?

A model that fails (1) or (2) on more than one of the 5-8 cases is not ready to be trusted unsupervised for this skill, regardless of how strong its general capability reputation is — these two are the specific behaviors this skill exists to enforce, not incidental quality issues.

## 2. On-chain token data interface

Already sanctioned and specified elsewhere in this deployment's own root instructions: all GMGN on-chain data goes through `gmgn-cli`, never through scraping `gmgn.ai` or any other method. This skill assumes that CLI (or an equivalent internal tool exposing the same fields) is already available to the runtime — no new configuration is introduced by this skill for that interface.

Fields this skill reads: `symbol`, `name`, `dev.creator_address`, `dev.creator_open_count`, `dev.creator_token_status`, `dev.cto_flag`, `dev.ath_token_info`, `dev.twitter_name_change_history`, `link.website`, `link.twitter_username`, `fee_distribution`, `launchpad`, `launchpad_platform`, and (optionally) the security fields `is_honeypot` / `buy_tax` / `sell_tax` / `is_renounced`.

## 3. Social search interface (pluggable)

This is the interface that replaces "browse X as a logged-in user," which this skill must never do. Configure exactly one backend:

| Config key | Value | Notes |
|---|---|---|
| `SOCIAL_SEARCH_PROVIDER` | `x_api` \| `grok_api` | Selects the backend. |
| `SOCIAL_SEARCH_API_KEY` | (secret, injected via the runtime's own secret store) | Never hardcoded in this skill or logged in its output. |

**Backend: `x_api`** — the official X API v2 search endpoints (server-to-server, no personal login involved). Map this skill's "recency-ordered" and "relevance/engagement-ordered" search requirement onto that API's `recent` search and its relevance-ranked mode respectively. Note the tier limits on how far back search can reach and on rate limits — these vary by X API access tier and should be checked against expected call volume before launch.

**Backend: `grok_api`** — xAI's Grok API called directly with an API key (not the browser-based `x.com/i/grok` chat surface, which is tied to a specific logged-in account and must not be used here). Grok's native X-grounded search can serve both the recency and relevance/engagement orderings this skill asks for; treat its synthesized summaries as leads only — this skill's own step 4 ("verify before you assert") still requires opening the actual post/page it points to, because a search provider's summary can misattribute content to the wrong project (a real failure mode observed during this skill's development, not a hypothetical one — see the retrospective note below).

**Minimal request/response contract** (either backend must be adaptable to this shape):

```
request:  { query: string, order: "recency" | "relevance", limit: int }
response: [{ author_handle: string, author_label: string | null,
             text: string, permalink: string, posted_at: timestamp,
             metrics: { views, likes, reposts, replies } }]
```

`author_label` carries a platform-applied account label when present (e.g. a parody/commentary/satire label) — surface it; don't drop it during any adaptation layer.

**This interface carries no content sanitization guarantee, unlike the on-chain interface.** `gmgn-cli` strips control/zero-width/bidi characters and flags instruction-shaped metadata before the skill ever sees it (see `SKILL.md`'s preamble). Neither X API v2 nor the Grok API does anything equivalent to post text — whatever an adaptation layer passes through in `text`, `author_handle`, and `author_label` is raw, unfiltered, attacker-controlled content, because that is exactly what a public post is. Do not add sanitization here that would mask a genuine attempt at prompting the model — pass the content through faithfully and let the model's own judgment (per `SKILL.md`'s hard rule on this) treat it as data rather than instructions.

**If neither backend is configured or the call fails:** do not fall back to guessing from the model's own prior knowledge, and do not fall back to browser automation or an unofficial scraper. Produce the card with 传播观察 stating plainly that social evidence could not be gathered this run, or decline the request if the user specifically needs the social half.

### Retrospective note on this interface

During this skill's development, a Grok-backed search for one token returned a confident, well-written origin story that — on independent verification — turned out to describe a *different, unrelated* token that merely shared a similar name. The error was caught only because the on-chain `link.website` field for the actual token in question was checked and compared against the domain the search result cited, and they didn't match. This is the concrete reason step 4 in `SKILL.md` treats any search provider's output as a lead requiring verification, never as a citable fact on its own — this applies to both backends equally and is not specific to either one.

## 4. What this skill must never do

- Use browser automation (headless or otherwise) to access any social platform.
- Assume, request, or depend on any individual person's authenticated session on any platform.
- Read local files, environment variables, or state beyond the two interfaces above and this skill's own reference files.
- Log, echo, or otherwise surface the value of `SOCIAL_SEARCH_API_KEY` or any other credential in a produced card, in an error message, or in any other user-visible output.
