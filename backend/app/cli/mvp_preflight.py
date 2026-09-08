from __future__ import annotations

import json

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_session_factory


def main() -> int:
    settings = get_settings()
    with get_session_factory()() as session:
        bind = session.get_bind()
        url = bind.url
        if bind.dialect.name != "postgresql":
            raise SystemExit("MVP demo requires PostgreSQL 16; refusing a non-PostgreSQL target.")
        server_version_num = int(session.scalar(text("show server_version_num")))
        if server_version_num < 160000:
            raise SystemExit(
                f"MVP demo requires PostgreSQL 16; found server_version_num={server_version_num}."
            )
        session.execute(text("select 1"))
        payload = {
            "status": "ok",
            "environment": settings.environment,
            "database_target": {
                "driver": url.drivername,
                "host": url.host or "local-socket",
                "port": url.port or 5432,
                "database": url.database,
                "server_major": server_version_num // 10000,
            },
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
