from __future__ import annotations

import queue
import threading
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

from core.config import Config
from .base import Base

from . import user
from . import bank_rec_model
from . import ledger_tax_models
from . import period_model

T = TypeVar("T")

_STOP = object()


class _DBThread(threading.Thread):

    def __init__(
        self, 
        db_url: 
        str, echo: bool = False
    ) -> None:
        super().__init__(name="db-worker", daemon=True)

        self.db_url = db_url
        self.echo = echo
        self.engine: Optional[Engine] = None
        
        self._session_factory: Optional[sessionmaker] = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._ready = threading.Event()

    def run(self) -> None:
        self.engine = create_engine(self.db_url, echo=self.echo, connect_args={})
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._ready.set()

        while True:
            item = self._queue.get()
            if item is _STOP:
                break

            fn, args, kwargs, future, needs_session = item

            try:
                if needs_session:
                    session = self._session_factory()
                    try:
                        result = fn(session, *args, **kwargs)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
                    finally:
                        session.close()
                else:
                    result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - forwarded to the caller via the Future
                if not future.cancelled():
                    future.set_exception(exc)
            else:
                if not future.cancelled():
                    future.set_result(result)

        if self.engine is not None:
            self.engine.dispose()

    def wait_until_ready(self) -> None:
        self._ready.wait()

    def submit(
        self, 
        fn: Callable, 
        *args: Any, 
        needs_session: bool = True, 
        **kwargs: Any
    ) -> Future:
        self.wait_until_ready()
        future: Future = Future()
        self._queue.put((fn, args, kwargs, future, needs_session))
        return future

    def stop(self) -> None:
        self._queue.put(_STOP)


class DatabaseManager:

    def __init__(
        self, 
        db_url: str, 
        echo: bool = False
    ) -> None:
        self.db_url = db_url
        self._thread = _DBThread(db_url, echo=echo)
        self._thread.start()
        self._thread.wait_until_ready()


    def run(
        self, 
        fn: Callable[..., T], 
        *args: Any, 
        timeout: Optional[float] = None, 
        **kwargs: Any
    ) -> T:
        future = self._thread.submit(
            fn, 
            *args, 
            needs_session=True, 
            **kwargs
        )
        return future.result(timeout=timeout)


    def execute_raw(
        self, 
        fn: Callable[..., T], 
        *args: Any, 
        timeout: Optional[float] = None, 
        **kwargs: Any
    ) -> T:
        future = self._thread.submit(
            fn, 
            *args, 
            needs_session=False, 
            **kwargs
        )
        return future.result(timeout=timeout)

    @contextmanager
    def session_scope(self) -> Iterator[SASession]:
        self._thread.wait_until_ready()
        session = self._thread._session_factory()  # noqa: SLF001
        
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


    def new_session(self) -> SASession:
        self._thread.wait_until_ready()
        return self._thread._session_factory()  # noqa: SLF001


    def init_db(self) -> None:
        def _init() -> None:
            engine = self._thread.engine
            inspector = inspect(engine)
            if inspector.has_table("sessions"):
                columns = {
                    column["name"] for column in inspector.get_columns("sessions")
                }
                if "id" not in columns:
                    self._migrate_legacy_sessions_table()
            Base.metadata.create_all(engine)

        self.execute_raw(_init)


    def _migrate_legacy_sessions_table(self) -> None:
        engine = self._thread.engine
        legacy_name = f"sessions_legacy_{uuid.uuid4().hex[:12]}"
        quote = engine.dialect.identifier_preparer.quote

        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {quote('sessions')} RENAME TO {quote(legacy_name)}"))

        Base.metadata.create_all(engine)

        legacy = Table(legacy_name, MetaData(), autoload_with=engine)
        new_table = Base.metadata.tables.get("sessions")
        if new_table is None:
            return

        new_columns = set(new_table.columns.keys())

        try:
            with engine.begin() as connection:
                rows = connection.execute(select(legacy)).mappings()
                for row in rows:
                    data = {k: v for k, v in dict(row).items() if k in new_columns}
                    data.setdefault("id", str(uuid.uuid4()))
                    connection.execute(new_table.insert().values(**data))
        except Exception:
            pass


    def drop_all(self) -> None:
        def _drop() -> None:
            Base.metadata.drop_all(self._thread.engine)

        self.execute_raw(_drop)


    def healthcheck(self) -> bool:
        def _check() -> bool:
            try:
                with self._thread.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return True
            except Exception:
                return False

        return self.execute_raw(_check)


    def dispose(self) -> None:
        self._thread.stop()
        self._thread.join(timeout=5)

