"""A `limits` storage backend that keeps counters in the app's own database.

Selected with RATELIMIT_STORAGE_URI = "appdb://". Each counter is one
rate_limit row, updated with a single atomic upsert, so concurrent Lambda
instances share counts correctly. Works on Postgres and SQLite (3.35+).

Only the fixed-window strategy (Flask-Limiter's default) is supported.
"""
import hashlib
import random
import time

from limits.storage import Storage
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .models import db

# Roughly one write in this many also deletes expired rows, so the table
# doesn't grow without a scheduled cleanup job.
_CLEANUP_ONE_IN = 50


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AppDatabaseStorage(Storage):
    STORAGE_SCHEME = ["appdb"]

    @property
    def base_exceptions(self):
        return SQLAlchemyError

    def incr(self, key, expiry, amount=1):
        now = time.time()
        # A separate connection and transaction, so counting is committed even
        # if the request's own work is rolled back (e.g. a failed login).
        with db.engine.begin() as conn:
            hits = conn.execute(
                text(
                    "INSERT INTO rate_limit (key_hash, hits, expires_at)"
                    " VALUES (:key, :amount, :new_expiry)"
                    " ON CONFLICT (key_hash) DO UPDATE SET"
                    "  hits = CASE WHEN rate_limit.expires_at <= :now"
                    "   THEN :amount ELSE rate_limit.hits + :amount END,"
                    "  expires_at = CASE WHEN rate_limit.expires_at <= :now"
                    "   THEN :new_expiry ELSE rate_limit.expires_at END"
                    " RETURNING hits"
                ),
                {"key": _hash(key), "amount": amount, "now": now, "new_expiry": now + expiry},
            ).scalar_one()
            if random.randrange(_CLEANUP_ONE_IN) == 0:
                conn.execute(text("DELETE FROM rate_limit WHERE expires_at <= :now"), {"now": now})
        return hits

    def get(self, key):
        with db.engine.connect() as conn:
            hits = conn.execute(
                text("SELECT hits FROM rate_limit WHERE key_hash = :key AND expires_at > :now"),
                {"key": _hash(key), "now": time.time()},
            ).scalar()
        return hits or 0

    def get_expiry(self, key):
        now = time.time()
        with db.engine.connect() as conn:
            expires_at = conn.execute(
                text("SELECT expires_at FROM rate_limit WHERE key_hash = :key AND expires_at > :now"),
                {"key": _hash(key), "now": now},
            ).scalar()
        return expires_at or now

    def check(self):
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def reset(self):
        with db.engine.begin() as conn:
            return conn.execute(text("DELETE FROM rate_limit")).rowcount

    def clear(self, key):
        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM rate_limit WHERE key_hash = :key"), {"key": _hash(key)})
