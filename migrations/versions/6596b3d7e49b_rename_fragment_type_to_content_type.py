"""rename fragment_type to content_type

EnrichmentFragmentType.fragment_type becomes content_type, and its enum type
(Postgres) / check constraint (SQLite) is renamed to match. Values are kept.

Revision ID: 6596b3d7e49b
Revises: a854f69c5fba
Create Date: 2026-10-01 01:03:22.982434

"""
from alembic import op
import sqlalchemy as sa

import api.models  # custom column types, e.g. api.models.UTCDateTime


# revision identifiers, used by Alembic.
revision = '6596b3d7e49b'
down_revision = 'a854f69c5fba'
branch_labels = None
depends_on = None

TABLE = "enrichment_fragment_type"
VALUES = ("text", "calendar")


def _rename(old, new):
    if op.get_bind().dialect.name == "postgresql":
        # Native enum type: rename the type and the column in place.
        op.execute(f"ALTER TYPE {old} RENAME TO {new}")
        op.alter_column(TABLE, old, new_column_name=new)
        return
    # SQLite: the enum is a CHECK constraint named after it ("<old> IN (...)"),
    # so rebuild the table with the column and its constraint renamed.
    with op.batch_alter_table(TABLE, recreate="always") as batch:
        batch.drop_constraint(old, type_="check")
        batch.alter_column(
            old,
            new_column_name=new,
            existing_nullable=False,
            existing_type=sa.Enum(*VALUES, name=old, create_constraint=False),
            type_=sa.Enum(*VALUES, name=new, create_constraint=True),
        )


def upgrade():
    _rename("fragment_type", "content_type")


def downgrade():
    _rename("content_type", "fragment_type")
