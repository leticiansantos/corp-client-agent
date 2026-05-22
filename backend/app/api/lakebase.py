"""
Storage backend — SQLite (local_db).

All persistent storage uses a local SQLite file (backend/local.db).
This module re-exports the public API of local_db so the rest of the
codebase can continue using `from app.api import lakebase`.
"""

from app.api.local_db import (  # noqa: F401
    execute,
    execute_one,
    ensure_schema,
    create_domain_schema,
    list_domain_schemas,
    migrate_domain,
)
