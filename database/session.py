from __future__ import annotations

import queue
import threading
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, TypeVar

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
from . import auth_model
from . import bank_rec_model
from . import ledger_tax_models
from . import period_model

T = TypeVar("T")

_STOP = object()


class _DBWorker(threading.Thread):
    """
    A dedicated background worker thread that continuously polls a task queue 
    for database operations and executes them sequentially within its thread context.
    """
    
    def __init__(
        self, 
        worker_id: int, 
        session_factory: sessionmaker, 
        task_queue: "queue.Queue[Any]"
    ) -> None:
        """
        Initializes the worker thread.

        Args:
            worker_id (int): A unique identifier for naming the thread (useful for debugging).
            session_factory (sessionmaker): The SQLAlchemy session factory bound to the engine.
            task_queue (queue.Queue): The shared thread-safe queue containing tasks to execute.
        """
        super().__init__(name=f"db-worker-{worker_id}", daemon=True)

        self._session_factory = session_factory
        self._queue = task_queue


    def run(self) -> None:
        """
        The main execution loop. It fetches tasks from the queue, executes the provided 
        function (with or without a managed session), handles commits/rollbacks, and 
        publishes the result (or exception) back to the caller's Future object.
        """
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


class DatabaseManager:
    """
    A thread-safe database manager utilizing a thread pool and message queue.
    Designed to safely handle high-concurrency database queries, optimized for 
    PostgreSQL connection pooling while maintaining strict execution safety.
    """

    def __init__(
        self,
        db_url: str,
        echo: bool = False,
        pool_workers: int = 1,
        pool_size: Optional[int] = None,
        max_overflow: int = 10,
    ) -> None:
        """
        Initializes the DatabaseManager, SQLAlchemy Engine, and worker pool.

        Args:
            db_url (str): The connection string for the database (e.g., postgresql+psycopg2://...).
            echo (bool): If True, SQLAlchemy will log all executed SQL statements.
            pool_workers (int): The number of background threads to spawn for concurrent execution.
            pool_size (Optional[int]): The maximum number of permanent connections in the SQLAlchemy pool.
            max_overflow (int): The maximum number of temporary connections allowed beyond pool_size.

        Raises:
            ValueError: If `pool_workers` < 1, or if attempting to use multiple workers 
                        with an in-memory SQLite database (which prevents cross-thread data sharing).
        """
        if pool_workers < 1:
            raise ValueError("pool_workers must be >= 1")

        is_sqlite = db_url.startswith("sqlite")
        if pool_workers > 1 and is_sqlite and ":memory:" in db_url:
            raise ValueError(
                "pool_workers > 1 is not supported with an in-memory SQLite "
                "database (sqlite:///:memory:) -- each connection gets its "
                "own independent database, so different workers would not "
                "see each other's data. Use pool_workers=1, a file-based "
                "SQLite database, or a real server database like Postgres."
            )

        self.db_url = db_url
        self.pool_workers = pool_workers

        engine_kwargs: dict = {"echo": echo, "connect_args": {}}
        if not is_sqlite:
            engine_kwargs["pool_size"] = pool_size if pool_size is not None else pool_workers
            engine_kwargs["max_overflow"] = max_overflow
            engine_kwargs["pool_pre_ping"] = True

        self.engine: Engine = create_engine(db_url, **engine_kwargs)
        self._session_factory: sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False)

        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._workers: List[_DBWorker] = [
            _DBWorker(i, self._session_factory, self._queue) 
            for i in range(pool_workers)
        ]
        for worker in self._workers:
            worker.start()


    def _dispatch(
        self, 
        fn: Callable, 
        args: tuple, 
        kwargs: dict, 
        needs_session: bool
    ) -> Future:
        """
        Internal helper method to package a task and submit it to the worker queue.

        Args:
            fn (Callable): The target function to execute.
            args (tuple): Positional arguments to pass to the function.
            kwargs (dict): Keyword arguments to pass to the function.
            needs_session (bool): Whether the worker should inject an active SQLAlchemy session.

        Returns:
            Future: A concurrent.futures.Future object to await the execution result.
        """
        future: Future = Future()
        self._queue.put((fn, args, kwargs, future, needs_session))
        return future


    def run(
        self, 
        fn: Callable[..., T], 
        *args: Any, 
        timeout: Optional[float] = None, 
        **kwargs: Any
    ) -> T:
        """
        Submit a database operation that requires an active session to the worker pool.

        Args:
            fn (Callable[..., T]): The function to execute. Its first argument MUST accept an SASession.
            *args (Any): Additional positional arguments for the function.
            timeout (Optional[float]): Maximum time in seconds to wait for a result.
            **kwargs (Any): Additional keyword arguments for the function.

        Returns:
            T: The result returned by the submitted function.
        """
        future = self._dispatch(fn, args, kwargs, needs_session=True)
        return future.result(timeout=timeout)


    def execute_raw(
        self, 
        fn: Callable[..., T], 
        *args: Any, 
        timeout: Optional[float] = None, 
        **kwargs: Any
    ) -> T:
        """
        Submit a database operation that DOES NOT require a session to the worker pool.
        (Useful for raw queries or schema definitions).

        Args:
            fn (Callable[..., T]): The function to execute.
            *args (Any): Positional arguments for the function.
            timeout (Optional[float]): Maximum time in seconds to wait for a result.
            **kwargs (Any): Keyword arguments for the function.

        Returns:
            T: The result returned by the submitted function.
        """
        future = self._dispatch(fn, args, kwargs, needs_session=False)
        return future.result(timeout=timeout)


    @contextmanager
    def session_scope(self) -> Iterator[SASession]:
        """
        A context manager providing transactional scope for manual database operations.
        Commits upon successful exit, or rolls back if an exception occurs.

        Yields:
            SASession: An active SQLAlchemy database session.
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> SASession:
        """
        Creates and returns a raw, unmanaged database session.
        The caller is strictly responsible for committing, rolling back, and closing this session.

        Returns:
            SASession: A new SQLAlchemy session instance.
        """
        return self._session_factory()


    def init_db(self) -> None:
        """
        Initializes the database schema.
        Creates all tables defined in the Base metadata. Checks for a legacy 'sessions'
        table and triggers a migration script if found.
        """
        def _init() -> None:
            engine = self.engine
            inspector = inspect(engine)
            if inspector.has_table("sessions"):
                columns = {column["name"] for column in inspector.get_columns("sessions")}
                if "id" not in columns:
                    self._migrate_legacy_sessions_table()
            Base.metadata.create_all(engine)

        self.execute_raw(_init)


    def _migrate_legacy_sessions_table(self) -> None:
        """
        Internal migration script to handle backwards compatibility for outdated 'sessions' tables.
        Renames the legacy table, provisions the new schema, and selectively copies matching 
        data over, generating UUIDs for missing primary keys.
        """
        engine = self.engine
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
        """
        WARNING: Destructive action.
        Drops all tables associated with the SQLAlchemy Base metadata.
        """
        def _drop() -> None:
            Base.metadata.drop_all(self.engine)

        self.execute_raw(_drop)


    def healthcheck(self) -> bool:
        """
        Tests the availability of the database by executing a lightweight query.

        Returns:
            bool: True if the database is reachable and responsive, False otherwise.
        """
        def _check() -> bool:
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return True
            except Exception:
                return False

        return self.execute_raw(_check)


    def dispose(self) -> None:
        """
        Gracefully shuts down the DatabaseManager.
        Sends termination signals to all worker threads, waits up to 5 seconds for them 
        to finish processing remaining queue items, and safely disposes the SQLAlchemy engine.
        """
        for _ in self._workers:
            self._queue.put(_STOP)

        for worker in self._workers:
            worker.join(timeout=5)
            
        self.engine.dispose()
