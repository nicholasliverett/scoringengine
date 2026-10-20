"""Add live service-state scoring snapshots.

The existing ``round_score`` table is retained unchanged. This migration adds
``service_score_snapshot`` for the fork's state-based scores and backfills its
timeline from completed checks.
"""

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011_add_exam_participation"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables_present = set(inspector.get_table_names())
    if "services" not in tables_present or "rounds" not in tables_present:
        return

    if "scoring_up" not in {column["name"] for column in inspector.get_columns("services")}:
        with op.batch_alter_table("services") as batch:
            batch.add_column(sa.Column("scoring_up", sa.Boolean(), nullable=False, server_default="0"))

    if "service_score_snapshot" not in tables_present:
        op.create_table(
            "service_score_snapshot",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("round_id", sa.Integer(), sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False),
            sa.Column("round_number", sa.Integer(), nullable=False),
            sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id"), nullable=False),
            sa.Column("service_points", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("round_id", "team_id", name="_service_score_snapshot_round_team_uc"),
        )
        op.create_index(
            "ix_service_score_snapshot_team_round", "service_score_snapshot", ["team_id", "round_number"]
        )
        op.create_index(
            "ix_service_score_snapshot_round_number", "service_score_snapshot", ["round_number"]
        )

    tables = sa.MetaData()
    services = sa.Table("services", tables, autoload_with=bind)
    checks = sa.Table("checks", tables, autoload_with=bind)
    rounds = sa.Table("rounds", tables, autoload_with=bind)
    snapshots = sa.Table("service_score_snapshot", tables, autoload_with=bind)

    # Seed the low-cost live-state cache from each service's most recent check.
    if "checks" in tables_present:
        bind.execute(sa.text(
            "UPDATE services SET scoring_up = COALESCE(("
            "  SELECT result FROM checks"
            "  WHERE checks.service_id = services.id AND checks.completed = 1"
            "  ORDER BY checks.round_id DESC, checks.id DESC LIMIT 1"
            "), 0)"
        ))

        # Build a sparse state timeline without modifying or deleting any prior
        # round_score rows. Each team's total changes only when one of its latest
        # service states changes.
        service_rows = bind.execute(sa.select(services.c.id, services.c.team_id, services.c.points)).all()
        service_team = {
            service_id: team_id for service_id, team_id, _ in service_rows if team_id is not None
        }
        service_points = {service_id: int(points or 0) for service_id, _, points in service_rows}
        service_up = {service_id: False for service_id in service_team}
        team_totals = {team_id: 0 for team_id in service_team.values()}
        written_totals = dict(team_totals)

        check_rows = iter(bind.execute(
            sa.select(rounds.c.number, checks.c.service_id, checks.c.result)
            .select_from(checks.join(rounds, checks.c.round_id == rounds.c.id))
            .where(checks.c.completed.is_(True))
            .order_by(rounds.c.number, checks.c.id)
            .execution_options(stream_results=True)
        ))
        next_check = next(check_rows, None)
        pending = []

        for round_id, round_number in bind.execute(
            sa.select(rounds.c.id, rounds.c.number).order_by(rounds.c.number, rounds.c.id)
        ):
            while next_check is not None and next_check[0] <= round_number:
                _, service_id, result = next_check
                if service_id in service_up:
                    was_up = service_up[service_id]
                    is_up = bool(result)
                    if was_up != is_up:
                        team_id = service_team[service_id]
                        points = service_points[service_id]
                        team_totals[team_id] += points if is_up else -points
                        service_up[service_id] = is_up
                next_check = next(check_rows, None)

            for team_id, points in team_totals.items():
                if points != written_totals[team_id]:
                    pending.append({
                        "round_id": round_id,
                        "round_number": round_number,
                        "team_id": team_id,
                        "service_points": points,
                    })
                    written_totals[team_id] = points
            if len(pending) >= 1000:
                bind.execute(snapshots.insert(), pending)
                pending.clear()

        if pending:
            bind.execute(snapshots.insert(), pending)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "service_score_snapshot" in tables:
        op.drop_index("ix_service_score_snapshot_round_number", table_name="service_score_snapshot")
        op.drop_index("ix_service_score_snapshot_team_round", table_name="service_score_snapshot")
        op.drop_table("service_score_snapshot")
    if "services" in tables and "scoring_up" in {
        column["name"] for column in inspector.get_columns("services")
    }:
        with op.batch_alter_table("services") as batch:
            batch.drop_column("scoring_up")
