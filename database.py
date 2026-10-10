"""Small, local SQLite store. One server process owns scheduling; no media in DB."""
import json
import os
from pathlib import Path
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            self.db.close()
            raise RuntimeError('Database schema is newer than this application. Refusing rollback.')
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT NOT NULL, updated REAL, PRIMARY KEY(kind,id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL)')
            self.db.execute('PRAGMA user_version=1')
        os.chmod(self.path, 0o600)

    def put(self, kind, key, data):
        # Runtime leases and cancellation flags are reconstructed after restart.
        value = {k: v for k, v in data.items() if k not in {'leases', '_persisted_at'}}
        with self.db:
            self.db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated',
                            (kind, key, json.dumps(value, ensure_ascii=False), time.time()))

    def get(self, kind, key):
        row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        return {r[0]: json.loads(r[1]) for r in self.db.execute('SELECT id,data FROM records WHERE kind=? ORDER BY updated', (kind,))}

    def delete(self, kind, key):
        with self.db:
            self.db.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key))

    def backup(self, path):
        # SQLite backup API includes committed WAL, unlike copying the .db alone.
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()
        os.chmod(path, 0o600)

    def close(self):
        self.db.close()
