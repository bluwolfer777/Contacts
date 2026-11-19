"""Persistenza su SQLite (file nel volume dati, condiviso da tutti gli utenti)."""
from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

DB_PATH = settings.data_dir / "contacts.db"
_lock = threading.Lock()

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS phones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    value       TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    value       TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (contact_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_phones_contact ON phones(contact_id);
CREATE INDEX IF NOT EXISTS ix_emails_contact ON emails(contact_id);
CREATE INDEX IF NOT EXISTS ix_ct_tag ON contact_tags(tag_id);
"""


def norm(value) -> str:
    """Minuscolo e senza accenti, per ricerche 'insensibili'."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.create_function("NRM", 1, norm, deterministic=True)
    conn.create_function("DIGITS", 1, digits, deterministic=True)
    return conn


_conn = _connect()
_conn.executescript(SCHEMA)


@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_list(values, lower=False) -> list[str]:
    out, seen = [], set()
    for v in values or []:
        v = str(v).strip()
        if not v:
            continue
        key = v.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(v.lower() if lower else v)
    return out


# ---------------------------------------------------------------- lettura

def _hydrate(conn, rows) -> list[dict]:
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    phones, emails, tags = {}, {}, {}
    for r in conn.execute(f"SELECT contact_id, value FROM phones WHERE contact_id IN ({marks}) ORDER BY position, id", ids):
        phones.setdefault(r["contact_id"], []).append(r["value"])
    for r in conn.execute(f"SELECT contact_id, value FROM emails WHERE contact_id IN ({marks}) ORDER BY position, id", ids):
        emails.setdefault(r["contact_id"], []).append(r["value"])
    for r in conn.execute(
        f"SELECT ct.contact_id, t.name FROM contact_tags ct JOIN tags t ON t.id = ct.tag_id "
        f"WHERE ct.contact_id IN ({marks}) ORDER BY t.name COLLATE NOCASE", ids):
        tags.setdefault(r["contact_id"], []).append(r["name"])
    return [
        {
            "id": r["id"], "name": r["name"], "notes": r["notes"],
            "phones": phones.get(r["id"], []), "emails": emails.get(r["id"], []), "tags": tags.get(r["id"], []),
            "updated_at": r["updated_at"], "updated_by": r["updated_by"],
        }
        for r in rows
    ]


def _token_clause(token: str) -> tuple[str, list]:
    """Condizione che è vera se il token compare in almeno un campo del contatto."""
    t = f"%{norm(token)}%"
    parts = [
        "NRM(c.name) LIKE ?",
        "NRM(c.notes) LIKE ?",
        "EXISTS (SELECT 1 FROM emails e WHERE e.contact_id = c.id AND NRM(e.value) LIKE ?)",
        "EXISTS (SELECT 1 FROM phones p WHERE p.contact_id = c.id AND NRM(p.value) LIKE ?)",
        "EXISTS (SELECT 1 FROM contact_tags ct JOIN tags g ON g.id = ct.tag_id WHERE ct.contact_id = c.id AND NRM(g.name) LIKE ?)",
    ]
    params = [t, t, t, t, t]
    d = digits(token)
    if len(d) >= 2:
        parts.append("EXISTS (SELECT 1 FROM phones p WHERE p.contact_id = c.id AND DIGITS(p.value) LIKE ?)")
        params.append(f"%{d}%")
    return "(" + " OR ".join(parts) + ")", params


def search(q: str = "", name: str = "", phone: str = "", email: str = "", notes: str = "",
           tags: list[str] | None = None, untagged: bool = False) -> list[dict]:
    where, params = [], []
    for token in (q or "").split():
        clause, p = _token_clause(token)
        where.append(clause)
        params += p
    if name.strip():
        where.append("NRM(c.name) LIKE ?")
        params.append(f"%{norm(name.strip())}%")
    if notes.strip():
        where.append("NRM(c.notes) LIKE ?")
        params.append(f"%{norm(notes.strip())}%")
    if email.strip():
        where.append("EXISTS (SELECT 1 FROM emails e WHERE e.contact_id = c.id AND NRM(e.value) LIKE ?)")
        params.append(f"%{norm(email.strip())}%")
    if phone.strip():
        d = digits(phone)
        if d:
            where.append("EXISTS (SELECT 1 FROM phones p WHERE p.contact_id = c.id AND (DIGITS(p.value) LIKE ? OR NRM(p.value) LIKE ?))")
            params += [f"%{d}%", f"%{norm(phone.strip())}%"]
        else:
            where.append("EXISTS (SELECT 1 FROM phones p WHERE p.contact_id = c.id AND NRM(p.value) LIKE ?)")
            params.append(f"%{norm(phone.strip())}%")
    for tag in _clean_list(tags):
        where.append("EXISTS (SELECT 1 FROM contact_tags ct JOIN tags g ON g.id = ct.tag_id "
                     "WHERE ct.contact_id = c.id AND g.name = ? COLLATE NOCASE)")
        params.append(tag)
    if untagged:
        where.append("NOT EXISTS (SELECT 1 FROM contact_tags ct WHERE ct.contact_id = c.id)")
    sql = "SELECT * FROM contacts c"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY NRM(c.name), c.id"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        return _hydrate(_conn, rows)


def get(contact_id: int) -> dict | None:
    with _lock:
        rows = _conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchall()
        items = _hydrate(_conn, rows)
    return items[0] if items else None


def list_tags() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT t.name, COUNT(ct.contact_id) AS n FROM tags t "
            "JOIN contact_tags ct ON ct.tag_id = t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        ).fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]


def count() -> int:
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]


# ---------------------------------------------------------------- scrittura

def _write_children(conn, cid: int, data: dict) -> None:
    conn.execute("DELETE FROM phones WHERE contact_id = ?", (cid,))
    conn.execute("DELETE FROM emails WHERE contact_id = ?", (cid,))
    conn.execute("DELETE FROM contact_tags WHERE contact_id = ?", (cid,))
    for i, v in enumerate(_clean_list(data.get("phones"))):
        conn.execute("INSERT INTO phones (contact_id, value, position) VALUES (?, ?, ?)", (cid, v, i))
    for i, v in enumerate(_clean_list(data.get("emails"))):
        conn.execute("INSERT INTO emails (contact_id, value, position) VALUES (?, ?, ?)", (cid, v, i))
    for tag in _clean_list(data.get("tags")):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        tid = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)", (cid, tid))
    # Rimuove i tag non più usati
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM contact_tags)")


def create(data: dict, user: str) -> int:
    now = _now()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO contacts (name, notes, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            (data["name"].strip(), (data.get("notes") or "").strip(), now, now, user),
        )
        cid = cur.lastrowid
        _write_children(conn, cid, data)
    return cid


def update(contact_id: int, data: dict, user: str) -> bool:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE contacts SET name = ?, notes = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (data["name"].strip(), (data.get("notes") or "").strip(), _now(), user, contact_id),
        )
        if cur.rowcount == 0:
            return False
        _write_children(conn, contact_id, data)
    return True


def delete(contact_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM contact_tags)")
        return cur.rowcount > 0
