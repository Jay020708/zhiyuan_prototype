"""Persist each browser's conversation history in the local SQLite database."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


UNTITLED = "新对话"
TITLE_LIMIT = 32


def _title(message):
    title = " ".join(message.split())
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT - 1].rstrip() + "…"
    return title or UNTITLED


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ChatHistory:
    def __init__(self, db_path):
        self.path = Path(db_path)

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS chat_history_conversations (
                    owner TEXT NOT NULL,
                    id TEXT NOT NULL,
                    case_name TEXT,
                    title TEXT,
                    updated_at TEXT NOT NULL,
                    state_json TEXT,
                    PRIMARY KEY (owner, id)
                );
                CREATE INDEX IF NOT EXISTS chat_history_recent ON chat_history_conversations (
                    owner, updated_at DESC, id DESC
                );
                CREATE TABLE IF NOT EXISTS chat_history_turns (
                    ordinal INTEGER PRIMARY KEY,
                    owner TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (owner, conversation_id) REFERENCES chat_history_conversations (owner, id)
                );
                CREATE INDEX IF NOT EXISTS chat_history_transcript ON chat_history_turns (owner, conversation_id);
            """)

    def create(self, owner, session_id, case=None):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT INTO chat_history_conversations (owner, id, case_name, updated_at) VALUES (?, ?, ?, ?)
            """, (owner, session_id, case, _now()))

    def get(self, owner, session_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            row = db.execute("""
                SELECT id, case_name, state_json FROM chat_history_conversations WHERE owner = ? AND id = ?
            """, (owner, session_id)).fetchone()
            if row is None:
                return None
            turns = db.execute("""
                SELECT payload_json FROM chat_history_turns WHERE owner = ? AND conversation_id = ? ORDER BY ordinal
            """, (owner, session_id))
            return {
                "id": row["id"],
                "case": row["case_name"],
                "turns": [json.loads(turn["payload_json"]) for turn in turns],
                "state": json.loads(row["state_json"]) if row["state_json"] is not None else None,
            }

    def list(self, owner):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT id, COALESCE(title, ?) AS title, updated_at FROM chat_history_conversations
                WHERE owner = ? ORDER BY updated_at DESC, id DESC
            """, (UNTITLED, owner))]

    def rename(self, owner, session_id, title):
        with closing(self._connect()) as db, db:
            updated = db.execute("""
                UPDATE chat_history_conversations SET title = ? WHERE owner = ? AND id = ?
            """, (_title(title), owner, session_id))
            return bool(updated.rowcount)

    def delete(self, owner, session_id):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                DELETE FROM chat_history_turns WHERE owner = ? AND conversation_id = ?
            """, (owner, session_id))
            deleted = db.execute("""
                DELETE FROM chat_history_conversations WHERE owner = ? AND id = ?
            """, (owner, session_id))
            return bool(deleted.rowcount)

    def append(self, owner, session_id, turn, state):
        """Save a sanitized turn and its workflow state in one transaction."""
        turn_json = json.dumps(turn, ensure_ascii=False)
        state_json = json.dumps(state, ensure_ascii=False)
        title = _title(turn["user"])
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""
                UPDATE chat_history_conversations SET title = COALESCE(title, ?), updated_at = ?, state_json = ?
                WHERE owner = ? AND id = ?
            """, (title, _now(), state_json, owner, session_id))
            if not updated.rowcount:
                return False
            db.execute("""
                INSERT INTO chat_history_turns (owner, conversation_id, payload_json) VALUES (?, ?, ?)
            """, (owner, session_id, turn_json))
        return True
