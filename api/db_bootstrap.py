"""One-time database setup, run as the RDS master user (the "bootstrap-db" command).

Creates the app's own login role (the user named in DATABASE_URL) and gives it
what it needs to run migrations and serve requests. After this, the app never
uses the master user: it connects as that role with IAM authentication.
Safe to run again.
"""
import os

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def bootstrap(master_password):
    url = make_url(os.environ["DATABASE_URL"])
    role = url.username
    master = os.environ["DB_MASTER_USER"]
    steps = []
    with psycopg.connect(
        host=url.host,
        port=url.port or 5432,
        dbname=url.database,
        user=master,
        password=master_password,
        sslmode=url.query.get("sslmode", "prefer"),
        connect_timeout=10,
        autocommit=True,
    ) as conn:
        ident = sql.Identifier(role)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]).fetchone():
            steps.append(f"role {role} already exists")
        else:
            # No password: it can only log in with IAM tokens (via rds_iam).
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(ident))
            steps.append(f"created role {role}")
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'").fetchone():
            conn.execute(sql.SQL("GRANT rds_iam TO {}").format(ident))
            steps.append("granted rds_iam (IAM login)")
        else:
            steps.append("rds_iam not present (not RDS); skipped")
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(url.database), ident)
        )
        # Postgres 15+ no longer lets every role create tables in public.
        conn.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(ident))
        steps.append(f"granted connect on {url.database} and create on schema public")
    return "; ".join(steps)
