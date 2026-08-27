"""Local SQLite store: draft history, recommendations, feedback, learned ADP.

This is where the bot gets better over time.  Every draft it watches
contributes (position, positional rank when taken, pick number) observations
that refit the ADP consumption curves, and every piece of feedback you give is
kept so strategy changes survive between sessions.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .adp import DEFAULT_ANCHORS, blend_anchors, fit_anchors_from_picks

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "ffbot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id    TEXT PRIMARY KEY,
    name        TEXT,
    teams       INTEGER,
    rounds      INTEGER,
    scoring     TEXT,
    superflex   INTEGER DEFAULT 0,
    dynasty     INTEGER DEFAULT 0,
    my_slot     INTEGER,
    strategy    TEXT,
    status      TEXT,
    is_mock     INTEGER DEFAULT 0,
    created_at  REAL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS picks (
    draft_id     TEXT,
    pick_no      INTEGER,
    round        INTEGER,
    slot         INTEGER,
    player_key   TEXT,
    name         TEXT,
    pos          TEXT,
    pos_rank     INTEGER,
    adp          REAL,
    PRIMARY KEY (draft_id, pick_no)
);
CREATE TABLE IF NOT EXISTS recommendations (
    draft_id   TEXT,
    pick_no    INTEGER,
    slot_rank  INTEGER,
    player_key TEXT,
    name       TEXT,
    pos        TEXT,
    score      REAL,
    payload    TEXT,
    created_at REAL,
    PRIMARY KEY (draft_id, pick_no, slot_rank)
);
CREATE TABLE IF NOT EXISTS choices (
    draft_id     TEXT,
    pick_no      INTEGER,
    player_key   TEXT,
    name         TEXT,
    followed     INTEGER,
    rec_position INTEGER,
    PRIMARY KEY (draft_id, pick_no)
);
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id   TEXT,
    created_at REAL,
    kind       TEXT,
    text       TEXT,
    payload    TEXT
);
CREATE TABLE IF NOT EXISTS adp_obs (
    draft_id  TEXT,
    teams     INTEGER,
    superflex INTEGER,
    pos       TEXT,
    pos_rank  INTEGER,
    pick_12   REAL,
    PRIMARY KEY (draft_id, pos, pos_rank)
);
CREATE TABLE IF NOT EXISTS player_bias (
    player_key TEXT PRIMARY KEY,
    delta      REAL,
    reason     TEXT,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_adp_obs_shape ON adp_obs (superflex, pos, pos_rank);
"""


@dataclass
class DraftSummary:
    draft_id: str
    name: str
    teams: int
    rounds: int
    scoring: str
    status: str
    my_slot: int | None
    created_at: float
    picks: int


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- drafts

    def upsert_draft(self, state, strategy_name: str = "", is_mock: bool = False) -> None:
        now = time.time()
        self.db.execute(
            """INSERT INTO drafts (draft_id,name,teams,rounds,scoring,superflex,
                                   dynasty,my_slot,strategy,status,is_mock,
                                   created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(draft_id) DO UPDATE SET
                 status=excluded.status, my_slot=excluded.my_slot,
                 strategy=excluded.strategy, updated_at=excluded.updated_at""",
            (state.draft_id, state.name, state.teams, state.rounds, state.scoring,
             int(state.superflex), int(state.is_dynasty), state.my_slot,
             strategy_name, state.status, int(is_mock), now, now),
        )
        self.db.commit()

    def list_drafts(self, limit: int = 25) -> list[DraftSummary]:
        rows = self.db.execute(
            """SELECT d.*, (SELECT COUNT(*) FROM picks p WHERE p.draft_id=d.draft_id) n
               FROM drafts d ORDER BY d.updated_at DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [DraftSummary(r["draft_id"], r["name"] or "", r["teams"], r["rounds"],
                             r["scoring"] or "", r["status"] or "", r["my_slot"],
                             r["created_at"] or 0.0, r["n"]) for r in rows]

    # -------------------------------------------------------------- picks

    def record_picks(self, state, resolve) -> int:
        """Persist picks and derive ADP-learning observations.

        `resolve(name, pos) -> (player_key, positional_rank)` maps a Sleeper
        pick onto our player universe.
        """
        rows = []
        obs = []
        seen_pos: dict[str, int] = {}
        for p in state.picks:
            key, prank = resolve(p.name, p.pos)
            rows.append((state.draft_id, p.pick_no, p.round, p.slot, key or "",
                         p.name, p.pos, prank, None))
            seen_pos[p.pos] = seen_pos.get(p.pos, 0) + 1
            # Market positional rank *as drafted* - this is what ADP measures.
            # Pick numbers are comparable across league sizes (the Nth player
            # off the board is the Nth-best), so no rescaling is applied.
            pick_12 = float(p.pick_no)
            obs.append((state.draft_id, state.teams, int(state.superflex),
                        p.pos, seen_pos[p.pos], pick_12))
        self.db.executemany(
            """INSERT INTO picks VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(draft_id,pick_no) DO UPDATE SET
                 player_key=excluded.player_key, name=excluded.name,
                 pos=excluded.pos, pos_rank=excluded.pos_rank""", rows)
        self.db.executemany(
            """INSERT INTO adp_obs VALUES (?,?,?,?,?,?)
               ON CONFLICT(draft_id,pos,pos_rank) DO UPDATE SET
                 pick_12=excluded.pick_12""", obs)
        self.db.commit()
        return len(rows)

    # ---------------------------------------------------- recommendations

    def record_recommendations(self, draft_id: str, pick_no: int,
                               recs: list[dict]) -> None:
        now = time.time()
        self.db.executemany(
            """INSERT INTO recommendations VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(draft_id,pick_no,slot_rank) DO UPDATE SET
                 player_key=excluded.player_key, name=excluded.name,
                 score=excluded.score, payload=excluded.payload""",
            [(draft_id, pick_no, i + 1, r.get("key", ""), r.get("name", ""),
              r.get("pos", ""), float(r.get("score", 0.0)),
              json.dumps(r, default=str), now) for i, r in enumerate(recs)])
        self.db.commit()

    def record_choice(self, draft_id: str, pick_no: int, player_key: str,
                      name: str) -> None:
        rows = self.db.execute(
            "SELECT slot_rank, player_key FROM recommendations "
            "WHERE draft_id=? AND pick_no=? ORDER BY slot_rank", (draft_id, pick_no)
        ).fetchall()
        rec_pos = next((r["slot_rank"] for r in rows if r["player_key"] == player_key), None)
        self.db.execute(
            """INSERT INTO choices VALUES (?,?,?,?,?,?)
               ON CONFLICT(draft_id,pick_no) DO UPDATE SET
                 player_key=excluded.player_key, name=excluded.name,
                 followed=excluded.followed, rec_position=excluded.rec_position""",
            (draft_id, pick_no, player_key, name,
             int(rec_pos == 1) if rec_pos else 0, rec_pos))
        self.db.commit()

    def agreement_report(self, draft_id: str | None = None) -> dict:
        q = ("SELECT c.*, r.name AS rec_name FROM choices c "
             "LEFT JOIN recommendations r ON r.draft_id=c.draft_id "
             "AND r.pick_no=c.pick_no AND r.slot_rank=1")
        args: tuple = ()
        if draft_id:
            q += " WHERE c.draft_id=?"
            args = (draft_id,)
        rows = self.db.execute(q, args).fetchall()
        n = len(rows)
        top1 = sum(1 for r in rows if r["rec_position"] == 1)
        top3 = sum(1 for r in rows if (r["rec_position"] or 99) <= 3)
        return {
            "picks_with_recs": n,
            "took_top_rec": top1,
            "took_top3_rec": top3,
            "top1_rate": top1 / n if n else 0.0,
            "top3_rate": top3 / n if n else 0.0,
            "divergences": [
                {"pick": r["pick_no"], "you_took": r["name"],
                 "bot_wanted": r["rec_name"], "rec_position": r["rec_position"]}
                for r in rows if (r["rec_position"] or 99) > 3
            ],
        }

    # ----------------------------------------------------------- feedback

    def add_feedback(self, draft_id: str | None, kind: str, text: str,
                     payload: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO feedback (draft_id,created_at,kind,text,payload) "
            "VALUES (?,?,?,?,?)",
            (draft_id, time.time(), kind, text, json.dumps(payload or {})))
        self.db.commit()
        return cur.lastrowid

    def list_feedback(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def set_player_bias(self, player_key: str, delta: float, reason: str) -> None:
        self.db.execute(
            """INSERT INTO player_bias VALUES (?,?,?,?)
               ON CONFLICT(player_key) DO UPDATE SET
                 delta=excluded.delta, reason=excluded.reason,
                 updated_at=excluded.updated_at""",
            (player_key, delta, reason, time.time()))
        self.db.commit()

    def player_biases(self) -> dict[str, float]:
        return {r["player_key"]: r["delta"]
                for r in self.db.execute("SELECT * FROM player_bias").fetchall()}

    # ------------------------------------------------------- learned ADP

    def learned_anchors(self, superflex: bool, min_drafts: int = 2
                        ) -> tuple[dict[str, list[tuple[float, float]]], int]:
        """Refit ADP consumption curves from observed drafts of this shape."""
        n_drafts = self.db.execute(
            "SELECT COUNT(DISTINCT draft_id) c FROM adp_obs WHERE superflex=?",
            (int(superflex),)).fetchone()["c"]
        if n_drafts < min_drafts:
            return {}, n_drafts

        rows = self.db.execute(
            "SELECT pos, pos_rank, pick_12 FROM adp_obs WHERE superflex=?",
            (int(superflex),)).fetchall()
        obs = [(r["pos"], int(r["pos_rank"]), int(round(r["pick_12"]))) for r in rows]
        learned = fit_anchors_from_picks(obs)

        # Shrink toward the prior until we have a healthy sample.
        weight = min(0.85, n_drafts / 8.0)
        out = {}
        for pos, pts in learned.items():
            prior = DEFAULT_ANCHORS.get(pos)
            out[pos] = blend_anchors(prior, pts, weight) if prior else pts
        return out, n_drafts
