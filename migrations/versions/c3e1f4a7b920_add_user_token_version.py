"""add app_user.token_version

Each API token carries the user's token_version and is accepted only while it
matches; logging out increments it to revoke the user's tokens. Existing rows
start at 0. Tokens issued before this migration carry no version, so they stop
working and their users sign in again.

Revision ID: c3e1f4a7b920
Revises: 6596b3d7e49b
Create Date: 2026-10-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3e1f4a7b920'
down_revision = '6596b3d7e49b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("app_user") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("app_user") as batch:
        batch.drop_column("token_version")
