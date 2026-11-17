"""Add student-facing exam labels to services."""

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "services" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("services")}
    if "exam_display" not in columns:
        with op.batch_alter_table("services") as batch:
            batch.add_column(
                sa.Column("exam_display", sa.String(length=16), nullable=False, server_default="finding")
            )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "services" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("services")}
    if "exam_display" in columns:
        with op.batch_alter_table("services") as batch:
            batch.drop_column("exam_display")
