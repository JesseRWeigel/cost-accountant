#!/usr/bin/env python3
"""Generate docs/index.html from the committed ledgers. No hand-written numbers.

Every figure on the page is read out of data/*.json, which is itself produced by the
extractor and gated by reconciliation. Rebuilding must be byte-for-byte reproducible, so
verify.sh rebuilds the page and compares; a page that drifts from its data is a stale
claim like any other.

The page carries no JavaScript at all. An inline script that fails to parse renders as
static HTML with no numbers in it and every unit test still passes, so the safest way to
guarantee the numbers are on the page is for the page to need nothing to display them.
"""
from __future__ import annotations

import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "index.html"
REPO = "https://github.com/JesseRWeigel/cost-accountant"

CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf7; --fg: #1b1a17; --muted: #5f5b52; --rule: #d8d3c7;
  --card: #ffffff; --accent: #8a4b2a; --accent-soft: #e8d9cf; --ok: #2f6b3f; --bad: #9b2c2c;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14130f; --fg: #eae6dc; --muted: #a09a8c; --rule: #33302a;
          --card: #1c1b16; --accent: #e0955f; --accent-soft: #3a2b22; --ok: #7dc48f; --bad: #e08585; }
}
:root[data-theme="dark"] {
  --bg: #14130f; --fg: #eae6dc; --muted: #a09a8c; --rule: #33302a;
  --card: #1c1b16; --accent: #e0955f; --accent-soft: #3a2b22; --ok: #7dc48f; --bad: #e08585;
}
:root[data-theme="light"] {
  --bg: #fbfaf7; --fg: #1b1a17; --muted: #5f5b52; --rule: #d8d3c7;
  --card: #ffffff; --accent: #8a4b2a; --accent-soft: #e8d9cf; --ok: #2f6b3f; --bad: #9b2c2c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 ui-serif, Georgia, "Iowan Old Style", serif; }
main { max-width: 60rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }
h1 { font-size: clamp(1.8rem, 5vw, 2.6rem); line-height: 1.15; margin: 0 0 .4rem; letter-spacing: -.02em; }
h2 { font-size: 1.35rem; margin: 3rem 0 .75rem; letter-spacing: -.01em; }
h3 { font-size: 1rem; margin: 1.75rem 0 .5rem; letter-spacing: .04em;
  text-transform: uppercase; color: var(--muted); font-weight: 600; }
p { margin: 0 0 1rem; max-width: 42rem; }
.lede { font-size: 1.1rem; color: var(--muted); max-width: 40rem; }
a { color: var(--accent); }
code, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: .88em; }
.tiles { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr)); margin: 2rem 0; }
.tile { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: .9rem 1rem; }
.tile .n { display: block; font-size: clamp(1.3rem, 4vw, 1.9rem); font-weight: 600;
  letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.tile .k { display: block; font-size: .78rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: .06em; margin-top: .15rem; }
.scroll { overflow-x: auto; border: 1px solid var(--rule); border-radius: 10px; background: var(--card); }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { padding: .5rem .75rem; text-align: left; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th { font-size: .72rem; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); font-weight: 600; }
tbody tr:last-child td { border-bottom: 0; }
td.num { text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, Menlo, monospace; font-size: .86rem; }
.barcell { min-width: 8rem; }
.bar { display: block; height: .55rem; border-radius: 3px; background: var(--accent); }
.bar-track { display: block; height: .55rem; border-radius: 3px; background: var(--accent-soft); }
.note { border-left: 3px solid var(--accent); padding: .1rem 0 .1rem 1rem; margin: 1.5rem 0;
  color: var(--muted); }
ul.checks { list-style: none; padding: 0; margin: 1rem 0; }
ul.checks li { padding: .35rem 0; border-bottom: 1px solid var(--rule); font-size: .92rem; }
ul.checks li:last-child { border-bottom: 0; }
.ok { color: var(--ok); font-weight: 600; }
.bad { color: var(--bad); font-weight: 600; }
footer { margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--rule);
  color: var(--muted); font-size: .88rem; }
"""


def usd(v: float) -> str:
    if v and abs(v) < 0.01:
        return f"${v:.4f}"
    return f"${v:,.2f}"


def esc(s) -> str:
    return html.escape(str(s))


def bar(fraction: float) -> str:
    pct = max(0.0, min(100.0, fraction * 100.0))
    return (f'<span class="bar-track"><span class="bar" style="width:{pct:.1f}%"></span></span>')


def tiles(led: dict) -> str:
    t = led["totals"]
    rows = [
        (usd(t["usd_total"]), "total billed"),
        (f"{t['stages']:,}", "API requests"),
        (f"{t['artifacts']:,}", "artifacts"),
        (f"{t['tokens']['all'] / 1e9:.2f}B", "tokens"),
        (f"{100 * t['tokens']['cache_read'] / t['tokens']['all']:.0f}%", "were cache reads"),
        (usd(t["usd_total"] / t["artifacts"]) if t["artifacts"] else "n/a", "per artifact"),
    ]
    cells = "".join(f'<div class="tile"><span class="n">{esc(n)}</span>'
                    f'<span class="k">{esc(k)}</span></div>' for n, k in rows)
    return f'<div class="tiles">{cells}</div>'


def medium_table(led: dict) -> str:
    rows = sorted(led["by_medium"].items(), key=lambda kv: -kv[1]["usd_amortised"])
    if not rows:
        return "<p>No artifacts were attributed.</p>"
    top = max(v["usd_amortised"] for _, v in rows) or 1.0
    body = "".join(
        "<tr>"
        f"<td>{esc(m)}</td>"
        f'<td class="num">{v["artifacts"]:,}</td>'
        f'<td class="num">{usd(v["usd_amortised"])}</td>'
        f'<td class="num">{usd(v["usd_per_artifact"])}</td>'
        f'<td class="num">{v["wall_seconds"] / 60:,.0f}</td>'
        f'<td class="barcell">{bar(v["usd_amortised"] / top)}</td>'
        "</tr>" for m, v in rows)
    return (
        '<div class="scroll"><table><thead><tr><th>medium</th><th>pieces</th>'
        "<th>total</th><th>per piece</th><th>minutes</th><th>share of spend</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>")


def receipt_table(led: dict, n: int = 15) -> str:
    rows = led["receipts"][:n]
    if not rows:
        return "<p>No receipts.</p>"
    body = "".join(
        "<tr>"
        f'<td class="mono">{esc(r["artifact"])}</td>'
        f"<td>{esc(r['medium'])}</td>"
        f'<td class="num">{r["stages"]:,}</td>'
        f'<td class="num">{r["tokens"]["output"]:,}</td>'
        f'<td class="num">{r["wall_seconds"] / 60:,.1f}</td>'
        f'<td class="num">{usd(r["usd_direct"])}</td>'
        f'<td class="num">{usd(r["usd_amortised"])}</td>'
        "</tr>" for r in rows)
    return (
        '<div class="scroll"><table><thead><tr><th>artifact</th><th>medium</th>'
        "<th>requests</th><th>output tokens</th><th>minutes</th><th>direct</th>"
        f"<th>with overhead</th></tr></thead><tbody>{body}</tbody></table></div>")


def token_table(led: dict) -> str:
    t = led["totals"]["tokens"]
    cm = led["pricing"]["cache_multipliers"]
    order = [("input", "1.00x input"), ("output", "output rate"),
             ("cache_read", f"{cm['read']}x input"),
             ("cache_write_5m", f"{cm['write_5m']}x input"),
             ("cache_write_1h", f"{cm['write_1h']}x input")]
    body = "".join(
        f"<tr><td>{esc(k.replace('_', ' '))}</td>"
        f'<td class="num">{t[k]:,}</td>'
        f'<td class="num">{100 * t[k] / t["all"]:.2f}%</td>'
        f"<td>{esc(rate)}</td></tr>" for k, rate in order)
    return ('<div class="scroll"><table><thead><tr><th>token class</th><th>count</th>'
            f"<th>share</th><th>priced at</th></tr></thead><tbody>{body}</tbody></table></div>")


def checks(led: dict) -> str:
    items = "".join(
        f'<li><span class="{"ok" if c["ok"] else "bad"}">'
        f'{"PASS" if c["ok"] else "FAIL"}</span> &nbsp;{esc(c["name"])} &nbsp;'
        f'<span style="color:var(--muted)">{esc(c["detail"])}</span></li>'
        for c in led["reconciliation"])
    return f'<ul class="checks">{items}</ul>'


def section(led: dict, heading: str, intro: str) -> str:
    t = led["totals"]
    unpriced = ""
    if t["unpriced_tokens"]:
        unpriced = (f'<p class="note"><strong>{t["unpriced_tokens"]:,} tokens could not be '
                    f'priced</strong> (models: {esc(", ".join(t["unpriced_models"]))}). '
                    "They are excluded from every dollar figure above rather than counted "
                    "as free.</p>")
    return f"""
<h2>{esc(heading)}</h2>
<p>{intro}</p>
{tiles(led)}
{unpriced}
<h3>Where the tokens went</h3>
{token_table(led)}
<h3>Cost per published piece, by medium</h3>
{medium_table(led)}
<h3>The most expensive artifacts</h3>
{receipt_table(led)}
<h3>Reconciliation</h3>
{checks(led)}
"""


def build() -> str:
    corpus = json.loads((ROOT / "data" / "ledger.corpus.json").read_text())
    parts = [section(
        corpus,
        "A real corpus",
        "Every number below comes from real Claude Code transcripts on one workstation: "
        "actual token counts from <code>message.usage</code>, actual timestamps. The "
        "transcripts themselves are not published. Artifact names are replaced by a hash "
        "of the path plus its extension, so the shape of the spend survives and the "
        "private project names do not.",
    )]
    self_path = ROOT / "data" / "ledger.self.json"
    if self_path.exists():
        me = json.loads(self_path.read_text())
        parts.append(section(
            me,
            "What this repository cost to build",
            "The same extractor, pointed at this project's own directory. These paths are "
            "not redacted because the repository is public, so the receipts name real "
            "files. This is the accountant billing itself.",
        ))

    t = corpus["totals"]
    direct_share = 100 * t["usd_direct"] / t["usd_total"] if t["usd_total"] else 0
    cache_share = 100 * t["tokens"]["cache_read"] / t["tokens"]["all"]
    flat = sum(m["usd_if_cache_tiers_ignored"] or 0 for m in t["by_model"].values())
    flat_ratio = flat / t["usd_total"] if t["usd_total"] else 0
    lede = (
        f"{t['stages']:,} API requests, {t['tokens']['all'] / 1e9:.2f} billion tokens, "
        f"{usd(t['usd_total'])}. Priced by token class, reconciled two independent ways, "
        "and honest about the part that cannot be attributed to any artifact."
    )
    findings = f"""
<h2>Three things the numbers say</h2>
<p><strong>{cache_share:.1f}% of the tokens are cache reads.</strong> Adding
<code>input_tokens</code>, <code>cache_read_input_tokens</code> and
<code>cache_creation_input_tokens</code> into one figure and multiplying by the input
rate gives <strong>{usd(flat)}</strong> for this corpus, {flat_ratio:.1f} times the real
{usd(t['usd_total'])}. That is not a rounding difference; it is the whole answer. Both
numbers are in <code>data/ledger.corpus.json</code>, per model, so this comparison can be
checked rather than taken on trust.</p>
<p><strong>A streamed turn writes its usage on every line.</strong> The corpus has
{t['stages']:,} distinct API requests spread across more usage-bearing lines than that,
each repeating the full usage object. Summing over lines roughly doubles the bill. The
extractor deduplicates by <code>requestId</code>.</p>
<p><strong>Only {direct_share:.1f}% of the money lands on a file.</strong>
{t['stages_with_artifacts']:,} of {t['stages']:,} requests wrote or edited anything; the
rest read, searched, ran commands and thought. That overhead is real and it is most of the
bill, so the per-artifact figures on this page show it amortised across the artifacts of
the same session, with the amount that has nowhere to go reported separately rather than
quietly dropped.</p>
"""

    body = f"""<main>
<h1>cost-accountant</h1>
<p class="lede">{esc(lede)}</p>
<p>A per-artifact cost and latency accountant for AI pipelines. It reads real usage out of
Claude Code transcripts, prices each token class at its own rate, attributes the spend to
the files that were produced, and emits a receipt per artifact.
<a href="{REPO}">Source on GitHub</a>.</p>
{findings}
{"".join(parts)}
<h2>Where the prices come from</h2>
<p>{esc(corpus['pricing']['source'])} Retrieved {esc(corpus['pricing']['retrieved'])}.
They live in <code>pricing.json</code> and are meant to be edited: list prices are not what
every account pays, batch requests are half price, and partner platforms are billed
separately. Cache multipliers used here: read
&times;{corpus['pricing']['cache_multipliers']['read']}, five-minute write
&times;{corpus['pricing']['cache_multipliers']['write_5m']}, one-hour write
&times;{corpus['pricing']['cache_multipliers']['write_1h']}.</p>
<p class="note">A dollar figure here is an estimate of list-price spend, not an invoice.
It cannot see negotiated rates, batch discounts, or anything billed outside the API.</p>
<footer>Generated by <code>scripts/build_docs.py</code> from
<code>data/ledger.corpus.json</code>. No number on this page was typed by hand.
Task MEDIA-047 from <a href="https://github.com/JesseRWeigel/722-things-to-build">722
things to build</a>. MIT.</footer>
</main>"""

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>cost-accountant: what each artifact actually cost</title>\n"
        f"<meta name=\"description\" content=\"{esc(lede)}\">\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    page = build()
    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
