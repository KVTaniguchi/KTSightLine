"""The addressal ledger.

The north-star metric. Cost and NPS are vanity; the real ones are addressal rate (Uber
sits around 67%), reply sentiment, and trajectories. Instrumented before there is a
single user, per the brief.

Every posted comment goes in. On each subsequent push we resolve prior comments: did the
flagged line change? was the thread resolved? what did the human reply? SQLite locally,
behind an interface so it can go somewhere real later.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class Addressal(StrEnum):
    OPEN = "open"  # posted, nothing has happened yet
    ADDRESSED = "addressed"  # the flagged code changed on a later push
    RESOLVED = "resolved"  # a human resolved the thread
    DISMISSED = "dismissed"  # explicitly rejected, or resolved without a code change
    STALE = "stale"  # the file or symbol went away


@dataclass(frozen=True)
class LedgerEntry:
    fingerprint: str
    repo: str
    pr_number: int
    skill_id: str
    file: str
    line: int
    claim: str
    comment_id: str | None
    posted_at: datetime
    head_sha: str
    status: Addressal = Addressal.OPEN
    resolved_at: datetime | None = None
    human_reply: str | None = None


class AddressalLedger(ABC):
    @abstractmethod
    def record_posted(self, entry: LedgerEntry) -> None: ...

    @abstractmethod
    def open_entries(self, repo: str, pr_number: int) -> list[LedgerEntry]: ...

    @abstractmethod
    def update_status(
        self,
        fingerprint: str,
        repo: str,
        pr_number: int,
        status: Addressal,
        *,
        human_reply: str | None = None,
    ) -> None: ...

    @abstractmethod
    def addressal_rate(self, repo: str | None = None, skill_id: str | None = None) -> float: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    fingerprint TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr_number   INTEGER NOT NULL,
    skill_id    TEXT NOT NULL,
    file        TEXT NOT NULL,
    line        INTEGER NOT NULL,
    claim       TEXT NOT NULL,
    comment_id  TEXT,
    posted_at   TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    status      TEXT NOT NULL,
    resolved_at TEXT,
    human_reply TEXT,
    PRIMARY KEY (fingerprint, repo, pr_number)
);
CREATE INDEX IF NOT EXISTS comments_by_skill ON comments(skill_id, status);
"""


class SqliteLedger(AddressalLedger):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            fingerprint=row["fingerprint"],
            repo=row["repo"],
            pr_number=row["pr_number"],
            skill_id=row["skill_id"],
            file=row["file"],
            line=row["line"],
            claim=row["claim"],
            comment_id=row["comment_id"],
            posted_at=datetime.fromisoformat(row["posted_at"]),
            head_sha=row["head_sha"],
            status=Addressal(row["status"]),
            resolved_at=(
                datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None
            ),
            human_reply=row["human_reply"],
        )

    def record_posted(self, entry: LedgerEntry) -> None:
        """Idempotent on (fingerprint, repo, pr).

        Re-posting the same finding on a later push must not create a second row — that
        would inflate the denominator of the metric the whole project is judged on.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO comments (fingerprint, repo, pr_number, skill_id, file, line,
                                      claim, comment_id, posted_at, head_sha, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(fingerprint, repo, pr_number) DO NOTHING
                """,
                (
                    entry.fingerprint,
                    entry.repo,
                    entry.pr_number,
                    entry.skill_id,
                    entry.file,
                    entry.line,
                    entry.claim,
                    entry.comment_id,
                    entry.posted_at.isoformat(),
                    entry.head_sha,
                    entry.status.value,
                ),
            )

    def already_posted(self, fingerprint: str, repo: str, pr_number: int) -> bool:
        """The dedupe check. Content-derived fingerprints make this survive rebases."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM comments WHERE fingerprint=? AND repo=? AND pr_number=?",
                (fingerprint, repo, pr_number),
            ).fetchone()
        return row is not None

    def open_entries(self, repo: str, pr_number: int) -> list[LedgerEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM comments WHERE repo=? AND pr_number=? AND status=?",
                (repo, pr_number, Addressal.OPEN.value),
            ).fetchall()
        return [self._to_entry(r) for r in rows]

    def update_status(
        self,
        fingerprint: str,
        repo: str,
        pr_number: int,
        status: Addressal,
        *,
        human_reply: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE comments SET status=?, resolved_at=?, human_reply=COALESCE(?, human_reply)
                   WHERE fingerprint=? AND repo=? AND pr_number=?""",
                (
                    status.value,
                    datetime.now(UTC).isoformat() if status is not Addressal.OPEN else None,
                    human_reply,
                    fingerprint,
                    repo,
                    pr_number,
                ),
            )

    def addressal_rate(self, repo: str | None = None, skill_id: str | None = None) -> float:
        """Addressed + resolved, over everything that has been decided.

        Open comments are excluded from the denominator: a PR nobody has looked at yet
        is not evidence that the bot was ignored.
        """
        clauses, params = ["status != ?"], [Addressal.OPEN.value]
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        if skill_id:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM comments WHERE {where}", params).fetchone()[
                0
            ]
            if not total:
                return 0.0
            good = conn.execute(
                f"SELECT COUNT(*) FROM comments WHERE {where} AND status IN (?,?)",
                [*params, Addressal.ADDRESSED.value, Addressal.RESOLVED.value],
            ).fetchone()[0]
        return good / total
