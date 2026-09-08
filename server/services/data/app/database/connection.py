# Data가 소유한 SQLite 연결과 트랜잭션, 스키마 변경 및 DB 백업을 관리한다.
"""SQLite connection ownership, migration, health, and backup."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        default_migrations = Path(__file__).parent / "migrations"
        self.migrations_dir = Path(migrations_dir or default_migrations)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            # 조회 후 갱신까지 쓰기 권한을 확보해, 두 작업자가 같은 대기 작업을 가져가지 않게 한다.
            # 중간에 예외가 나면 전체를 취소하고, 정상 종료할 때만 변경을 확정한다.
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # WAL은 변경 내용을 별도 로그에 기록하여 읽기와 쓰기가 서로 막히는 시간을 줄인다.
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for migration in sorted(self.migrations_dir.glob("*.sql")):
                if migration.name in applied:
                    continue
                sql = migration.read_text(encoding="utf-8")
                version = migration.name.replace("'", "''")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations(version, applied_at) "
                    + f"VALUES ('{version}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                    + "COMMIT;"
                )

    def health(self) -> dict[str, object]:
        with self.connection() as connection:
            connection.execute("SELECT 1").fetchone()
            foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
        return {
            "foreign_keys": foreign_keys,
            "journal_mode": journal_mode,
        }

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            destination_connection = sqlite3.connect(target)
            try:
                # 실행 중인 DB는 파일 복사 대신 SQLite 백업 기능으로 일관된 사본을 만든다.
                source.backup(destination_connection)
            finally:
                destination_connection.close()
        return target
