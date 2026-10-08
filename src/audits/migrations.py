"""Migraciones SQL de las Auditorias ISO 50001, aplicadas con el runner
generico de `shared/db_migrations.py`.

Son la migracion de la tabla `users` (que `auth.py` crea con
`CREATE TABLE IF NOT EXISTS`, idempotente para bases NUEVAS pero incapaz de
anadir columnas a una existente) seguida de las del modulo, en ese orden:
`audit_projects.owner_id` y `audit_project_members.user_id` referencian
`users`, asi que tiene que estar migrada antes.

`auth_0001_users_name_and_role` es aqui una version recortada de la del
chatbot: reconstruye `users` y `daily_usage`, pero no `chats`, que no existe
en esta base de datos. El nombre se conserva a proposito — las dos
herramientas compartian `data/tambora.db` y `scripts/split_db.py` copia
`schema_migrations` entera, asi que renombrarla haria que se reaplicara
sobre unos `users` ya migrados y se perderian `first_name`/`last_name`.
"""

from shared.db_migrations import Migration, apply_migrations

from .core.migrations import AUDIT_MIGRATIONS

_SQL_AUTH_0001 = """
CREATE TABLE users_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    first_name    TEXT NOT NULL DEFAULT '',
    last_name     TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL CHECK(role IN ('admin','user','sge')),
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE daily_usage_new (
    user_id INTEGER NOT NULL,
    date    TEXT NOT NULL,
    count   INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

INSERT INTO users_new (id, username, password_hash, role, is_active, created_at)
    SELECT id, username, password_hash, role, is_active, created_at FROM users;

INSERT INTO daily_usage_new (user_id, date, count)
    SELECT user_id, date, count FROM daily_usage;

DROP TABLE daily_usage;
DROP TABLE users;

ALTER TABLE users_new RENAME TO users;
ALTER TABLE daily_usage_new RENAME TO daily_usage;

CREATE INDEX IF NOT EXISTS idx_usage         ON daily_usage(user_id, date);
"""

MIGRATIONS: list[Migration] = [
    ("auth_0001_users_name_and_role", _SQL_AUTH_0001),
    *AUDIT_MIGRATIONS,
]


async def run(db_path) -> list[str]:
    return await apply_migrations(db_path, MIGRATIONS)
