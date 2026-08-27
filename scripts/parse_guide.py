"""Parse the extracted text of Joel Smyth's 2026 Draft Guide into structured JSON.

The source PDF lays its rankings out in multi-column tables, so pdf text
extraction emits rank numbers and player names in separate runs.  This script
recovers the structure using stable *anchors* (known first/last player names)
rather than raw line offsets, so it survives minor re-extraction differences.

Output: data/guide_2026.json
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "raw" / "joel_smyth_2026_guide.txt"
OUT = ROOT / "data" / "guide_2026.json"

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}

POS_PREFIX = re.compile(r"^(QB|RB|WR|TE|K|DEF)\s+(.*)$")
INT_LINE = re.compile(r"^\d+$")
FLOAT_LINE = re.compile(r"^\d+\.\d+$")

# Lines that appear inside the ranking tables but are not player names.
TABLE_NOISE = {
    "Positional Rankings", "PPR", "half-PPR", "Half-PPR", "PPR Big Board",
    "Half-PPR Big Board", "Quarterbacks Running Backs Wide Receivers Tight Ends",
    "Target", "I'll Pass", "Avoiding", "Last Update:", "August 16th",
    "'25 Adjusted PPG", "Rk QB", "Rk TE", "Rk RB", "adj", "PPG Reason...",
    "Rk RB adj PPG Reason... Rk WR adj PPG Reason...",
}


def clean(s: str) -> str:
    for lig, rep in LIGATURES.items():
        s = s.replace(lig, rep)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s+", " ", s).strip()
    # "T .J. Hockenson" -> "T.J. Hockenson"
    s = re.sub(r"\b([A-Z]) \.", r"\1.", s)
    return s


def load_pages() -> dict[int, list[str]]:
    text = SRC.read_text(encoding="utf-8")
    pages: dict[int, list[str]] = {}
    current = None
    for raw in text.splitlines():
        m = re.match(r"^===== PAGE (\d+) =====$", raw.strip())
        if m:
            current = int(m.group(1))
            pages[current] = []
            continue
        if current is None:
            continue
        line = clean(raw)
        if line:
            pages[current].append(line)
    return pages


def names_on(page: list[str]) -> list[str]:
    """Player-name lines: not pure numbers, not table chrome."""
    out = []
    for line in page:
        if INT_LINE.match(line) or FLOAT_LINE.match(line):
            continue
        if line in TABLE_NOISE:
            continue
        out.append(line)
    return out


def parse_big_board(page: list[str]) -> list[dict]:
    """Big boards render 150 entries as `POS Name`.

    Extraction order is columns 2+3 first (ranks 51-150) then column 1
    (ranks 1-50).  Verified against both the PPR (p4) and half-PPR (p6) boards.
    """
    entries = []
    for line in page:
        m = POS_PREFIX.match(line)
        if m:
            entries.append((m.group(1), clean(m.group(2))))
    assert len(entries) == 150, f"expected 150 big-board entries, got {len(entries)}"
    tail, head = entries[:100], entries[100:]
    ordered = head + tail  # ranks 1-50 then 51-150
    return [
        {"rank": i + 1, "pos": pos, "name": name}
        for i, (pos, name) in enumerate(ordered)
    ]


def parse_positional(page: list[str]) -> dict[str, list[str]]:
    """Positional pages emit RB(60), QB(32), WR(60), TE(32) in that order."""
    names = names_on(page)
    assert len(names) == 184, f"expected 184 positional names, got {len(names)}"
    return {
        "RB": names[0:60],
        "QB": names[60:92],
        "WR": names[92:152],
        "TE": names[152:184],
    }


def parse_adj_ppg(page: list[str], anchor: str, count: int) -> list[dict]:
    """Read `count` names starting at `anchor`, then the next `count` floats."""
    idx = next(i for i, l in enumerate(page) if l == anchor)
    names = [l for l in page[idx: idx + count]]
    values: list[float] = []
    for line in page[idx + count:]:
        if FLOAT_LINE.match(line):
            values.append(float(line))
            if len(values) == count:
                break
    assert len(values) == count, f"{anchor}: got {len(values)}/{count} values"
    return [
        {"name": n, "adj_ppg": v, "rank": i + 1}
        for i, (n, v) in enumerate(zip(names, values))
    ]


def parse_rookies(page: list[str]) -> list[dict]:
    out = []
    for line in page:
        m = re.match(r"^(\d+)\.\s*([A-Z]{2,3})?\s*(QB|RB|WR|TE)?\s*(.+)$", line)
        if m and m.group(4):
            rank = int(m.group(1))
            if rank > 40:
                continue
            out.append({
                "rank": rank,
                "team": m.group(2),
                "pos": m.group(3),
                "name": clean(m.group(4)),
            })
    return sorted(out, key=lambda r: r["rank"])


def parse_top50(pages: dict[int, list[str]]) -> list[dict]:
    joined = " ".join(pages[20] + pages[21])
    # Stats are numbered 50 down to 1.
    # Lookbehind (not a consuming group) so back-to-back matches like
    # "...with 82. 7. 35 'passing' QBs..." don't swallow the next delimiter.
    hits = list(re.finditer(r"(?:(?<=\s)|^)(\d{1,2})\.\s", joined))
    out = []
    for i, m in enumerate(hits):
        num = int(m.group(1))
        if not 1 <= num <= 50:
            continue
        start = m.end()
        end = hits[i + 1].start() if i + 1 < len(hits) else len(joined)
        body = clean(joined[start:end])
        if len(body) < 25:
            continue
        out.append({"n": num, "text": body})
    # de-dup, keep the longest capture per stat number
    best: dict[int, dict] = {}
    for s in out:
        if s["n"] not in best or len(s["text"]) > len(best[s["n"]]["text"]):
            best[s["n"]] = s
    return [best[k] for k in sorted(best, reverse=True)]


STRATEGY = {
    "source": "Page 11 - My Draft Strategy (12-team PPR baseline)",
    "round_by_round": {
        1: "RB", 2: "RB", 3: "WR", 4: "BPA", 5: "WR", 6: "BPA", 7: "BPA",
        8: "QB", 9: "Upside WR", 10: "Punt TE", 11: "Top Handcuff",
        12: "Upside QB", 13: "Favorite Deep Sleeper", 14: "D/ST", 15: "Kicker/IR",
    },
    "positional": {
        "QB": {
            "main_target": "ADP QB7-QB11; snipe a favorite that falls, usually 2-3 left in Round 8",
            "secondary": "Late rushing QB - '70% of the time it works every time'; Purdy/Nix count as volume QBs",
            "note": "QB3-6 are going WAY later in 2026; still prefer to wait, but a big faller is worth it (QB3 2026 = 55th overall)",
            "target_adp_pos_rank": [7, 11],
        },
        "RB": {
            "main_target": "Get 3 RBs from the top ~25-30 RBs",
            "note": "RB/RB start is great - elite league winners come from Rounds 1-2 90% of the time",
            "fade": "RB30-RB40 is mostly a waste of time compared to QB/WR/TE in that same range",
            "target_top_n": 30,
            "target_count": 3,
            "dead_zone_pos_rank": [30, 40],
        },
        "WR": {
            "main_target": "Round 3 and Round 5 WR range - prefer over most RBs there",
            "fade": "WR5-WR12 range is not a favorite vs both the RBs in that round and WRs a round later",
            "note": "WRs are the most ideal position when hunting upside late in drafts",
            "sweet_spot_rounds": [3, 5],
            "fade_pos_rank": [5, 12],
        },
        "TE": {
            "main_target": "Wait for best value - want TE when it is genuinely best-player-available",
            "note": "Like TE2-TE4 if stuck with no favorite RB/WR; Rd 7/8 grab one of the last mid-round TEs once RB/WR dries up; punting completely works",
            "fade": "Rather go backup-RB hunting than grab a TE2, or even D/ST and K in some leagues",
        },
    },
    "rules": [
        {"n": 1, "rule": "Don't draft off rankings without understanding ADP",
         "detail": "Use rankings to find value vs ADP, not as raw draft order."},
        {"n": 2, "rule": "Don't beat ADP",
         "detail": "The goal isn't to draft RB30 and have them finish RB29 - prefer the riskier, higher-upside player when true value potential is higher."},
        {"n": 3, "rule": "No K or D/ST until the last two rounds"},
        {"n": 4, "rule": "Good 'process players' late",
         "detail": "Rookie WRs, rushing QBs, talent on top offenses, cemented RB2s/handcuffs at the end of drafts."},
        {"n": 5, "rule": "Balance risk", "detail": "Don't draft three boom/bust or injury-risk players on the same team."},
        {"n": 6, "rule": "Early waivers and post-draft waivers matter the most they will all season"},
    ],
}


def main() -> None:
    pages = load_pages()
    guide = {
        "title": "Joel Smyth's Draft Guide 2026",
        "last_update": "2026-08-16",
        "big_board": {
            "ppr": parse_big_board(pages[4]),
            "half_ppr": parse_big_board(pages[6]),
        },
        "positional": {
            "ppr": parse_positional(pages[5]),
            "half_ppr": parse_positional(pages[7]),
        },
        "adj_ppg_2025": {
            "QB": parse_adj_ppg(pages[12], "Josh Allen", 32),
            "TE": parse_adj_ppg(pages[12], "Trey McBride", 28),
            "RB": parse_adj_ppg(pages[13], "Christian McCaffrey", 46),
            "WR": parse_adj_ppg(pages[13], "Puka Nacua", 48),
        },
        "dynasty_rookies": parse_rookies(pages[8]),
        "top_50_stats": parse_top50(pages),
        "strategy": STRATEGY,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(guide, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {OUT}")
    for fmt in ("ppr", "half_ppr"):
        bb = guide["big_board"][fmt]
        print(f"  {fmt:9s} big board: {len(bb)}  #1={bb[0]['name']}  "
              f"#50={bb[49]['name']}  #51={bb[50]['name']}  #150={bb[149]['name']}")
    for pos, rows in guide["positional"]["ppr"].items():
        print(f"  ppr {pos}: {len(rows):3d}  1={rows[0]}  last={rows[-1]}")
    for pos, rows in guide["adj_ppg_2025"].items():
        print(f"  adjPPG {pos}: {len(rows):3d}  top={rows[0]['name']} {rows[0]['adj_ppg']}")
    print(f"  rookies: {len(guide['dynasty_rookies'])}, stats: {len(guide['top_50_stats'])}")


if __name__ == "__main__":
    main()
