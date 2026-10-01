#!/usr/bin/env python3
"""段级翻译缓存（v2.6.0）：SQLite 按 SHA256(源文本+语向+模型) 键控。

batch 模式引擎调用前查缓存，命中即跳过 LLM——同模板医学文献批量场景
减少 30-50% LLM 调用。缓存永不过期（翻译结果确定性强），可手动清除。
"""

from __future__ import annotations

import hashlib
import sqlite3
import os

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm (
    key TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    lang_pair TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


class TranslationMemory:
    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(source_text: str, lang_pair: str, model: str) -> str:
        return hashlib.sha256(
            (source_text + "\x00" + lang_pair + "\x00" + model).encode("utf-8")
        ).hexdigest()

    def get(self, source_text: str, lang_pair: str, model: str):
        k = self._key(source_text, lang_pair, model)
        row = self._db.execute(
            "SELECT translated_text FROM tm WHERE key = ?", (k,)).fetchone()
        if row:
            self.hits += 1
            return row[0]
        self.misses += 1
        return None

    def put(self, source_text: str, translated_text: str,
            lang_pair: str, model: str):
        k = self._key(source_text, lang_pair, model)
        self._db.execute(
            "INSERT OR REPLACE INTO tm (key, source_text, translated_text, "
            "lang_pair, model) VALUES (?,?,?,?,?)",
            (k, source_text, translated_text, lang_pair, model))
        self._db.commit()

    def close(self):
        self._db.close()

    def clear(self):
        self._db.execute("DELETE FROM tm")
        self._db.commit()

    @property
    def size(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
