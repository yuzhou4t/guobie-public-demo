import ipaddress
import os
from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    operation_url = os.getenv("GUOBIE_DATABASE_OPERATION_URL", "").strip()
    database_url = operation_url or get_settings().database_url
    engine_url = _database_url_with_hostaddrs(
        database_url,
        configured=(os.getenv("GUOBIE_DATABASE_OPERATION_HOSTADDRS", "") if operation_url else None),
        include_session_options=not bool(operation_url),
    )
    return _create_engine(database_url, engine_url)


@lru_cache
def get_advisory_lock_engine() -> Engine:
    database_url = get_settings().database_url
    if not os.getenv("GUOBIE_DATABASE_OPERATION_URL", "").strip():
        return get_engine()
    return _create_engine(database_url, _database_url_with_hostaddrs(database_url))


def _create_engine(database_url: str, engine_url: URL) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        url = make_url(database_url)
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database in {None, "", ":memory:"}:
            kwargs["poolclass"] = StaticPool
        kwargs.pop("pool_pre_ping", None)
    engine = create_engine(engine_url, **kwargs)
    if database_url.startswith("sqlite"):
        event.listen(
            engine,
            "connect",
            lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"),
        )
    return engine


def _database_url_with_hostaddrs(
    database_url: str,
    *,
    configured: str | None = None,
    include_session_options: bool = True,
):
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if configured is None:
        configured = os.getenv("GUOBIE_DATABASE_HOSTADDRS", "")
    configured = configured.strip()
    if not configured or not database_url.startswith("postgresql") or not url.host:
        return url

    addresses: list[str] = []
    for value in configured.split(","):
        address = value.strip()
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("GUOBIE_DATABASE_HOSTADDRS must contain IP addresses") from exc
        if not parsed.is_global:
            raise ValueError("GUOBIE_DATABASE_HOSTADDRS must contain public IP addresses")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return url

    query = dict(url.query)
    query["host"] = ",".join([url.host] * len(addresses))
    query["hostaddr"] = ",".join(addresses)
    query.setdefault("connect_timeout", "5")
    query.setdefault("keepalives", "1")
    query.setdefault("keepalives_idle", "15")
    query.setdefault("keepalives_interval", "5")
    query.setdefault("keepalives_count", "3")
    query.setdefault("tcp_user_timeout", "30000")
    if include_session_options:
        query.setdefault("options", "-c statement_timeout=120000 -c lock_timeout=10000")
    else:
        query.pop("options", None)
    return url.set(query=query)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with get_session_factory()() as session:
        yield session


def reset_database_caches() -> None:
    get_session_factory.cache_clear()
    engines: list[Engine] = []
    if get_engine.cache_info().currsize:
        engines.append(get_engine())
    if get_advisory_lock_engine.cache_info().currsize:
        lock_engine = get_advisory_lock_engine()
        if lock_engine not in engines:
            engines.append(lock_engine)
    for engine in engines:
        engine.dispose()
    get_engine.cache_clear()
    get_advisory_lock_engine.cache_clear()
    get_settings.cache_clear()
