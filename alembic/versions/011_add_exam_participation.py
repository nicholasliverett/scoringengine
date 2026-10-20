"""Add per-team exam credentials and participation credit."""

from alembic import op
import sqlalchemy as sa


revision = "011_add_exam_participation"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "exam_credential",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", name="uq_exam_credential_team"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "exam_participation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("score_adjustment_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["score_adjustment_id"], ["score_adjustment.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("team_id", name="uq_exam_participation_team"),
        sa.UniqueConstraint("score_adjustment_id"),
    )


def downgrade():
    op.drop_table("exam_participation")
    op.drop_table("exam_credential")
