import os

import click
from flask import Flask
from flask.cli import with_appcontext
from flask_migrate import Migrate
from sqlalchemy import select

from . import ratelimit_storage  # noqa: F401  (registers the appdb:// storage scheme)
from .models import User, db

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _flag(name, default):
    return os.environ.get(name, default) in ("1", "true", "True")


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        # Local default: a SQLite file in instance/. Only then is anything
        # written to disk (Lambda's code directory is read-only).
        os.makedirs(app.instance_path, exist_ok=True)
        database_url = "sqlite:///" + os.path.join(app.instance_path, "volunteerscheduler.db")
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI=database_url,
        # Create missing tables on startup. Convenient locally; deployed
        # environments set AUTO_CREATE_TABLES=0 and run migrations instead.
        AUTO_CREATE_TABLES=_flag("AUTO_CREATE_TABLES", "1"),
        # Signs auth tokens. Set a long random API_SECRET_KEY outside development,
        # distinct from the web tier's SECRET_KEY.
        SECRET_KEY=os.environ.get("API_SECRET_KEY", "dev-api"),
        # Token lifetime in seconds: how long an admin sign-in lasts (8h).
        AUTH_TOKEN_MAX_AGE=int(os.environ.get("AUTH_TOKEN_MAX_AGE", 8 * 60 * 60)),
        # Failed logins allowed per account name.
        LOGIN_RATE_LIMIT=os.environ.get("LOGIN_RATE_LIMIT", "5 per minute;20 per hour"),
        # Login attempts allowed per client IP (successful or not).
        LOGIN_IP_RATE_LIMIT=os.environ.get("LOGIN_IP_RATE_LIMIT", "10 per minute;20 per hour"),
        # Header carrying the browser's IP, set by the web tier. Only set this
        # where the API can't be called directly (in Lambda it's invoked only by
        # the web function), or clients could forge it.
        TRUSTED_CLIENT_IP_HEADER=os.environ.get("TRUSTED_CLIENT_IP_HEADER") or None,
        # Counters live in the app database so all instances share them.
        RATELIMIT_STORAGE_URI=os.environ.get("RATELIMIT_STORAGE_URI", "appdb://"),
        RATELIMIT_HEADERS_ENABLED=True,
    )
    if database_url.startswith("postgresql"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            # A Lambda instance handles one request at a time, so 1 is enough
            # there; keeps RDS connection count low.
            "pool_size": int(os.environ.get("DB_POOL_SIZE", 5)),
            "max_overflow": 0,
            # Connections idle between invocations may have been dropped.
            "pool_pre_ping": True,
            "pool_recycle": 300,
            # No server-side prepared statements, so connection poolers that
            # share backends (RDS Proxy, PgBouncer in transaction mode) work.
            "connect_args": {"prepare_threshold": None},
        }

    db.init_app(app)
    if _flag("DB_IAM_AUTH", "0"):
        from .db_auth import enable_iam_auth

        with app.app_context():
            enable_iam_auth(db.engine)
    Migrate(app, db, directory=MIGRATIONS_DIR)
    if app.config["AUTO_CREATE_TABLES"]:
        with app.app_context():
            db.create_all()

    from . import admin, routes
    from .errors import ApiError, handle_api_error
    app.register_error_handler(ApiError, handle_api_error)
    routes.limiter.init_app(app)

    # Registered after the limiter, so it runs before the limiter's own
    # after-request hook (Flask runs these in reverse). That hook records
    # failed logins on a fresh connection; ending the request's transaction
    # first returns its connection to the pool, so a pool of 1 (Lambda) can't
    # wait on itself. Routes commit their own writes, so this only ends reads.
    @app.after_request
    def _release_db_connection(response):
        db.session.close()
        return response

    app.register_blueprint(routes.bp, url_prefix="/api")
    app.register_blueprint(admin.bp, url_prefix="/api/admin")
    from . import event_config
    app.register_blueprint(event_config.bp, url_prefix="/api/admin/event-config")
    from . import event_manage
    app.register_blueprint(event_manage.bp, url_prefix="/api/admin/event-manage")

    app.cli.add_command(create_user)

    return app


@click.command("create-user")
@with_appcontext
@click.argument("name")
@click.option("--superuser", is_flag=True, help="Give the user superuser rights.")
@click.password_option(help="Prompted for (hidden, with confirmation) if omitted.")
def create_user(name, superuser, password):
    """Create a login user. The password is hashed here, server-side."""
    if db.session.scalar(select(User).filter_by(name=name)) is not None:
        raise click.ClickException(f"user {name!r} already exists")
    if problem := User.password_problem(password):
        raise click.ClickException(problem)
    user = User(name=name, is_superuser=superuser)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"created user {name!r}" + (" (superuser)" if superuser else ""))
