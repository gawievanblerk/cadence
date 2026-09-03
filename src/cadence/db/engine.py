from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.db.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def make_database(url: str, *, create: bool = True) -> Database:
    db = Database(url)
    if create:
        db.create_all()
    return db
