"""PANINI loop guards. Only host-verified progress resets history."""

# Bounded runaway detection and headless supervision for PANINI-CLI.
# Adapted from MiniMax Code runaway-guard (MIT); see licenses/minimax-code.MIT.txt.

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections import Counter, deque


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class LoopGuard:
    def __init__(self, repeat_limit=4, action_limit=100):
        self.repeat_limit, self.action_limit = repeat_limit, action_limit
        self.history = deque(maxlen=12)
        self.count = 0
        self.progress = None
        self.secret = secrets.token_bytes(32)
        self.errors = deque(maxlen=12)
        self.polls = {}

    def _fingerprint(self, value):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        # Raw tool data never enters guard history. Hash streaming-sized outputs first.
        return hmac.new(self.secret, hashlib.sha256(encoded).digest(), hashlib.sha256).hexdigest()

    def verified_progress(self, fingerprint):
        if fingerprint and fingerprint != self.progress:
            self.history.clear()
            self.errors.clear()
            self.polls.clear()
            self.progress = fingerprint

    def observe(self, action, result, *, error_family=None, polling_key=None):
        self.count += 1
        if self.count > self.action_limit:
            return "action_budget"
        pair = (self._fingerprint(action), self._fingerprint(result))
        self.history.append(pair)
        if error_family:
            self.errors.append(self._fingerprint(error_family))
            if Counter(self.errors)[self.errors[-1]] >= self.repeat_limit:
                return "same_error_family"
        if polling_key is not None:
            key = self._fingerprint(polling_key)
            previous, count = self.polls.get(key, (None, 0))
            self.polls[key] = (pair[1], count + 1 if previous == pair[1] else 1)
            if self.polls[key][1] >= self.repeat_limit:
                return "polling_repeat"
            if len(self.polls) > 32:
                self.polls.pop(next(iter(self.polls)))
        if Counter(self.history)[pair] >= self.repeat_limit:
            return "repeated_action_and_result"
        if len(self.history) >= 8:
            tail = list(self.history)[-8:]
            if tail[:2] * 4 == tail and tail[0] != tail[1]:
                return "alternating_action_cycle"
        errors = [r for a, r in self.history if a == pair[0]]
        if len(errors) >= self.repeat_limit * 2:
            return "repeated_action_without_verified_progress"
        if Counter(r for _, r in self.history)[pair[1]] >= self.repeat_limit * 2:
            return "repeated_result_without_verified_progress"
        return None


class EventJournal:
    """Cursor replay is read-only. Reconnecting never repeats an execution request."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (cursor INTEGER PRIMARY KEY, run TEXT NOT NULL, event_key TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run,event_key))"
        )
        self.db.commit()

    def append(self, run, key, value):
        data = json.dumps(value, ensure_ascii=False)
        if len(data.encode()) > 64 * 1024:
            raise ValueError("Event exceeds journal limit")
        with self.db:
            row = self.db.execute(
                "INSERT OR IGNORE INTO events(run,event_key,payload) VALUES(?,?,?)",
                (run, key, data),
            )
        return row.rowcount == 1

    def replay(self, run, after=0, limit=200):
        rows = self.db.execute(
            "SELECT cursor,payload FROM events WHERE run=? AND cursor>? ORDER BY cursor LIMIT ?",
            (run, after, max(1, min(limit, 1000))),
        )
        return [{"cursor": cursor, **json.loads(payload)} for cursor, payload in rows]

    def close(self):
        self.db.close()
