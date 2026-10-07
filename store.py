"""SQLite cache for scraped products and candidate listings (stdlib only)."""
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

import catalog
from catalog import Candidate
from schema import DocumentRecord, ModeRecord, ProductRecord, RawSpec

DEFAULT_DB = Path(__file__).parent / "data" / "cache.db"
PRODUCT_TTL = timedelta(days=7)
CANDIDATE_TTL = timedelta(days=1)
# Bump whenever adapter/parser output changes: rows written under another version are ignored (and
# overwritten on the next scrape). '2' invalidates every row cached before the parser fixes.
PARSER_VERSION = "5"  # 5: rating/review_count/is_new/release_date signals; 4: cooking sub key scr -> sco and reclassified cooking adapters; 3: full spec tables + image_url

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
    url TEXT PRIMARY KEY, brand TEXT, model_number TEXT, spec_json TEXT, docs_json TEXT,
    raw_specs_json TEXT, modes_json TEXT, fetched_at TEXT, content_hash TEXT);
CREATE TABLE IF NOT EXISTS candidates(
    key TEXT PRIMARY KEY, candidates_json TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS seen_models(
    url TEXT PRIMARY KEY, brand TEXT, country TEXT, sub TEXT, major TEXT, model_number TEXT, name TEXT,
    price_usd REAL, price_local REAL, currency TEXT, attrs_json TEXT, first_seen TEXT, last_seen TEXT,
    baseline INTEGER);
CREATE TABLE IF NOT EXISTS seen_groups(
    brand TEXT, country TEXT, sub TEXT, complete_at TEXT, PRIMARY KEY(brand, country, sub));
CREATE TABLE IF NOT EXISTS schedules(
    id TEXT PRIMARY KEY, name TEXT, config_json TEXT, interval_h REAL, enabled INTEGER, created_at TEXT,
    last_run TEXT, next_run TEXT, last_status TEXT, last_summary_json TEXT);
"""


@dataclass
class CachedProduct:
    product: ProductRecord
    documents: list[DocumentRecord]
    raw_specs: list[RawSpec]
    modes: Optional[list[ModeRecord]]  # None = modes never extracted for this entry
    fetched_at: str


def _dump(models) -> str:
    return json.dumps([m.model_dump() for m in models], ensure_ascii=False)


def _load(model, text: str) -> list:
    return [model.model_validate(d) for d in json.loads(text)]


_CORRUPT = (ValueError, ValidationError, json.JSONDecodeError, TypeError)  # ValidationError/JSONDecodeError are ValueErrors


def _stale_sub(sub: Optional[str]) -> bool:
    """True for a stored sub key the catalog no longer knows (e.g. the retired 'scr')."""
    return bool(sub) and not catalog.is_sub(sub)


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c, c:
            c.executescript(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(products)")}
            if "parser_version" not in cols:  # migrate pre-versioned caches; their rows (NULL) never match
                c.execute("ALTER TABLE products ADD COLUMN parser_version TEXT")

    def _conn(self) -> sqlite3.Connection:
        # short-lived connection per call: safe across the Streamlit worker thread
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _fresh(fetched_at: str, ttl: timedelta, now: datetime) -> bool:
        return now - datetime.fromisoformat(fetched_at) <= ttl

    # products
    def put_product(self, url: str, product: ProductRecord, documents, raw_specs,
                    modes: Optional[list[ModeRecord]] = None, now: Optional[datetime] = None) -> None:
        """Store a freshly scraped product (resets fetched_at). Use update_modes to add modes only."""
        now = now or datetime.now()
        spec = product.model_dump_json()
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT OR REPLACE INTO products(url, brand, model_number, spec_json, docs_json, raw_specs_json,"
                " modes_json, fetched_at, content_hash, parser_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (url, product.brand, product.model_number, spec, _dump(documents), _dump(raw_specs),
                 None if modes is None else _dump(modes), now.isoformat(timespec="seconds"),
                 hashlib.sha256(spec.encode()).hexdigest(), PARSER_VERSION))

    def update_modes(self, url: str, modes: list[ModeRecord]) -> None:
        """Attach extracted modes to an existing row without touching fetched_at of the scraped data."""
        with closing(self._conn()) as c, c:
            c.execute("UPDATE products SET modes_json=? WHERE url=? AND parser_version=?",
                      (_dump(modes), url, PARSER_VERSION))

    def update_product(self, url: str, product: ProductRecord) -> None:
        """Replace the stored ProductRecord (e.g. a re-downloaded image_path) without touching fetched_at."""
        spec = product.model_dump_json()
        with closing(self._conn()) as c, c:
            c.execute("UPDATE products SET spec_json=?, content_hash=? WHERE url=? AND parser_version=?",
                      (spec, hashlib.sha256(spec.encode()).hexdigest(), url, PARSER_VERSION))

    def get_product(self, url: str, ttl: timedelta = PRODUCT_TTL,
                    now: Optional[datetime] = None) -> Optional[CachedProduct]:
        """Fresh cached product, else None. Unreadable/legacy/other-parser-version rows count as a miss."""
        now = now or datetime.now()
        with closing(self._conn()) as c:
            row = c.execute("SELECT spec_json, docs_json, raw_specs_json, modes_json, fetched_at, parser_version "
                            "FROM products WHERE url=?", (url,)).fetchone()
        if row is None or row[5] != PARSER_VERSION:
            return None
        try:
            if not self._fresh(row[4], ttl, now):
                return None
            product = ProductRecord.model_validate_json(row[0])
            if _stale_sub(product.subcategory):
                return None  # retired sub key (e.g. 'scr'): re-scrape
            return CachedProduct(product, _load(DocumentRecord, row[1]),
                                 _load(RawSpec, row[2]), None if row[3] is None else _load(ModeRecord, row[3]), row[4])
        except _CORRUPT:
            return None  # self-healing: the caller re-scrapes and overwrites the bad row

    # candidate listings
    @staticmethod
    def _key(brand: str, category: str, limit: Optional[int] = None, subcategory: Optional[str] = None,
             country: str = "us") -> str:
        suffix = "" if country == "us" else f"|{country}"  # US keys stay as before (existing cache rows remain valid)
        return f"v{PARSER_VERSION}|{brand}|{category}|{subcategory or '*'}|{limit}{suffix}"

    def put_candidates(self, brand: str, category: str, candidates: list[Candidate],
                       now: Optional[datetime] = None, limit: Optional[int] = None,
                       subcategory: Optional[str] = None, country: str = "us") -> None:
        if not candidates:  # an empty listing is usually a transient failure; never pin it for the TTL
            return
        now = now or datetime.now()
        with closing(self._conn()) as c, c:
            c.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                      (self._key(brand, category, limit, subcategory, country), _dump(candidates), now.isoformat(timespec="seconds")))

    def get_candidates(self, brand: str, category: str, ttl: timedelta = CANDIDATE_TTL,
                       now: Optional[datetime] = None, limit: Optional[int] = None,
                       subcategory: Optional[str] = None, country: str = "us") -> Optional[list[Candidate]]:
        now = now or datetime.now()
        with closing(self._conn()) as c:
            row = c.execute("SELECT candidates_json, fetched_at FROM candidates WHERE key=?",
                            (self._key(brand, category, limit, subcategory, country),)).fetchone()
        if row is None:
            return None
        try:
            if not self._fresh(row[1], ttl, now):
                return None
            cands = _load(Candidate, row[0])
            return None if any(_stale_sub(c.subcategory) for c in cands) else (cands or None)
        except _CORRUPT:
            return None

    # durable discovery history (never expires): the pool the competitor match works on
    def record_seen(self, candidates: list[Candidate], limit: Optional[int] = None,
                    now: Optional[datetime] = None) -> int:
        """Remember every listed model with first/last seen times. Returns the number of NEW discoveries.

        A model is a new discovery (baseline=0) only when its (brand, country, sub) group already had a COMPLETE
        listing before (a listing shorter than `limit` reached the end of the catalogue). Everything first seen
        while the group is still incomplete or unbaselined is baseline=1: a bigger `limit` later must not make old
        models look like launches."""
        now_s = (now or datetime.now()).isoformat(timespec="seconds")
        groups: dict[tuple[str, str, str], list[Candidate]] = {}
        for c in candidates:
            if c.subcategory:
                groups.setdefault((c.brand, c.country, c.subcategory), []).append(c)
        new = 0
        with closing(self._conn()) as conn, conn:
            for (brand, country, sub), items in groups.items():
                had_complete = conn.execute("SELECT 1 FROM seen_groups WHERE brand=? AND country=? AND sub=?",
                                            (brand, country, sub)).fetchone() is not None
                for c in items:
                    row = conn.execute("SELECT attrs_json, price_usd, price_local FROM seen_models WHERE url=?",
                                       (c.url,)).fetchone()
                    if row is None:
                        baseline = 0 if had_complete else 1
                        new += baseline == 0
                        conn.execute("INSERT INTO seen_models VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (c.url, brand, country, sub, c.category, c.model_number, c.name, c.price_usd,
                                      c.price_local, c.currency, json.dumps(c.attrs, ensure_ascii=False), now_s, now_s,
                                      baseline))
                    else:
                        attrs = {**json.loads(row[0] or "{}"), **c.attrs}
                        conn.execute("UPDATE seen_models SET sub=?, major=?, name=?, price_usd=?, price_local=?,"
                                     " currency=?, attrs_json=?, last_seen=? WHERE url=?",
                                     (sub, c.category, c.name, c.price_usd if c.price_usd is not None else row[1],
                                      c.price_local if c.price_local is not None else row[2], c.currency,
                                      json.dumps(attrs, ensure_ascii=False), now_s, c.url))
                if limit is not None and len(items) < limit and not had_complete:
                    conn.execute("INSERT OR IGNORE INTO seen_groups VALUES(?,?,?,?)", (brand, country, sub, now_s))
        return new

    def update_seen_attrs(self, url: str, attrs: dict) -> None:
        """Merge facts learned from a detail scrape (rating, release date, real width ...) into the history row."""
        with closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT attrs_json FROM seen_models WHERE url=?", (url,)).fetchone()
            if row is not None and attrs:
                merged = {**json.loads(row[0] or "{}"), **attrs}
                conn.execute("UPDATE seen_models SET attrs_json=? WHERE url=?",
                             (json.dumps(merged, ensure_ascii=False), url))

    def list_seen(self, sub: Optional[str] = None, major: Optional[str] = None, country: Optional[str] = None,
                  brands: Optional[list[str]] = None) -> list[dict]:
        """History rows (dicts: Candidate fields + attrs, first_seen, last_seen, baseline) for the given filters."""
        where, args = [], []
        for col, val in (("sub", sub), ("major", major), ("country", country)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if brands:
            where.append("brand IN (%s)" % ",".join("?" * len(brands)))
            args.extend(brands)
        sql = ("SELECT url, brand, country, sub, major, model_number, name, price_usd, price_local, currency,"
               " attrs_json, first_seen, last_seen, baseline FROM seen_models")
        with closing(self._conn()) as conn:
            rows = conn.execute(sql + (" WHERE " + " AND ".join(where) if where else ""), args).fetchall()
        keys = ("url", "brand", "country", "sub", "major", "model_number", "name", "price_usd", "price_local",
                "currency", "attrs", "first_seen", "last_seen", "baseline")
        out = []
        for r in rows:
            d = dict(zip(keys, r))
            try:
                d["attrs"] = json.loads(d["attrs"] or "{}")
            except json.JSONDecodeError:
                d["attrs"] = {}
            out.append(d)
        return out

    def backfill_seen(self) -> int:
        """Seed the history from candidate listings cached before it existed (all baseline: their launch dates
        are unknown). Returns the number of models added. Safe to call repeatedly."""
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT key, candidates_json, fetched_at FROM candidates").fetchall()
        added = 0
        for key, text, fetched_at in rows:
            parts = key.split("|")  # v<ver>|brand|category|sub|limit[|country]
            if len(parts) < 5:
                continue
            country = parts[5] if len(parts) > 5 else "us"
            try:
                cands = [c.model_copy(update={"country": country, "subcategory": c.subcategory or parts[3]})
                         for c in _load(Candidate, text)]
            except _CORRUPT:
                continue
            with closing(self._conn()) as conn:
                before = conn.execute("SELECT COUNT(*) FROM seen_models").fetchone()[0]
            self.record_seen(cands, limit=None, now=datetime.fromisoformat(fetched_at))  # limit=None: never completes
            with closing(self._conn()) as conn:
                added += conn.execute("SELECT COUNT(*) FROM seen_models").fetchone()[0] - before
        return added

    def count_new_discoveries(self) -> int:
        """Models first seen after their group had a complete baseline (what a scheduled re-search is looking for)."""
        with closing(self._conn()) as conn:
            return conn.execute("SELECT COUNT(*) FROM seen_models WHERE baseline=0").fetchone()[0]

    # scheduled re-searches (server-driven, see scheduler.py)
    _SCHEDULE_COLS = ("id", "name", "config_json", "interval_h", "enabled", "created_at", "last_run", "next_run",
                      "last_status", "last_summary_json")

    @classmethod
    def _schedule_row(cls, r) -> dict:
        d = dict(zip(cls._SCHEDULE_COLS, r))
        d["config"] = json.loads(d.pop("config_json") or "{}")
        summary = d.pop("last_summary_json")
        d["last_summary"] = json.loads(summary) if summary else None
        d["enabled"] = bool(d["enabled"])
        return d

    def create_schedule(self, schedule_id: str, name: str, config: dict, interval_h: float, next_run: str,
                        now: Optional[datetime] = None) -> dict:
        created = (now or datetime.now()).isoformat(timespec="seconds")
        with closing(self._conn()) as conn, conn:
            conn.execute("INSERT INTO schedules(id, name, config_json, interval_h, enabled, created_at, next_run)"
                         " VALUES(?,?,?,?,1,?,?)",
                         (schedule_id, name, json.dumps(config, ensure_ascii=False), interval_h, created, next_run))
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        with closing(self._conn()) as conn:
            row = conn.execute(f"SELECT {', '.join(self._SCHEDULE_COLS)} FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._schedule_row(row) if row else None

    def list_schedules(self) -> list[dict]:
        with closing(self._conn()) as conn:
            rows = conn.execute(f"SELECT {', '.join(self._SCHEDULE_COLS)} FROM schedules ORDER BY created_at, id").fetchall()
        return [self._schedule_row(r) for r in rows]

    def update_schedule(self, schedule_id: str, **fields) -> None:
        """Set enabled / last_run / next_run / last_status / last_summary (dict) on a schedule."""
        cols = {"enabled": lambda v: int(bool(v)), "last_run": str, "next_run": str, "last_status": str,
                "last_summary": lambda v: json.dumps(v, ensure_ascii=False)}
        sets, args = [], []
        for key, val in fields.items():
            if key not in cols:
                raise ValueError(f"cannot update {key!r}")
            sets.append("last_summary_json=?" if key == "last_summary" else f"{key}=?")
            args.append(cols[key](val))
        if sets:
            with closing(self._conn()) as conn, conn:
                conn.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id=?", (*args, schedule_id))

    def delete_schedule(self, schedule_id: str) -> bool:
        with closing(self._conn()) as conn, conn:
            return conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)).rowcount > 0
