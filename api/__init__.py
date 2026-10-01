import os

import click
from flask import Flask
from flask.cli import with_appcontext
from sqlalchemy import select

from .models import User, db


def create_app():
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL",
            "sqlite:///" + os.path.join(app.instance_path, "volunteerscheduler.db"),
        ),
    )

    db.init_app(app)
    with app.app_context():
        db.create_all()

    from . import routes
    app.register_blueprint(routes.bp, url_prefix="/api")

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
    if not password:
        raise click.ClickException("password must not be empty")
    user = User(name=name, is_superuser=superuser)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"created user {name!r}" + (" (superuser)" if superuser else ""))
