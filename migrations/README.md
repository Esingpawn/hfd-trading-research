# Database migrations

Alembic migrations live in `migrations/versions`.

Use the configured `DATABASE_URL` from the environment. If it is missing, Alembic falls back to the local SQLite development database.

Common commands:

```bash
alembic upgrade head
alembic revision --autogenerate -m "message"
```
