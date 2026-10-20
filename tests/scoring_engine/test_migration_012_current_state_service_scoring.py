"""Tests for migration 012's additive current-state score backfill."""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from scoring_engine.db import db

_MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "alembic",
    "versions",
    "012_current_state_service_scoring.py",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_012", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration():
    return _load_migration()


def test_upgrade_adds_state_column_and_sparse_snapshots_without_changing_round_score(tmp_path, migration):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/legacy.db")
    db.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE service_score_snapshot"))
        conn.execute(sa.text("ALTER TABLE services DROP COLUMN scoring_up"))
        conn.execute(sa.text("INSERT INTO teams (id, name, color) VALUES (1, 'Blue', 'Blue')"))
        conn.execute(sa.text(
            "INSERT INTO services (id, name, check_name, team_id, points, host, port) VALUES "
            "(1, 'SSH', 'SSHCheck', 1, 100, 'host', 22), "
            "(2, 'Web', 'HTTPCheck', 1, 50, 'host', 80)"
        ))
        conn.execute(sa.text("INSERT INTO rounds (id, number) VALUES (1, 1), (2, 2)"))
        conn.execute(sa.text(
            "INSERT INTO checks (id, round_id, service_id, result, completed) VALUES "
            "(1, 1, 1, 1, 1), (2, 1, 2, 1, 1), "
            "(3, 2, 1, 0, 1), (4, 2, 2, 1, 1)"
        ))
        # An existing legacy score row is intentionally preserved by revision 012.
        conn.execute(sa.text(
            "INSERT INTO round_score (round_id, round_number, team_id, service_points, flag_points) "
            "VALUES (1, 1, 1, 999, 0)"
        ))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

        assert "scoring_up" in {c["name"] for c in sa.inspect(conn).get_columns("services")}
        states = conn.execute(sa.text("SELECT id, scoring_up FROM services ORDER BY id")).all()
        snapshots = conn.execute(sa.text(
            "SELECT round_number, service_points FROM service_score_snapshot ORDER BY round_number"
        )).all()
        old_scores = conn.execute(sa.text("SELECT service_points FROM round_score")).all()

    engine.dispose()
    assert states == [(1, 0), (2, 1)]
    assert snapshots == [(1, 150), (2, 50)]
    assert old_scores == [(999,)]
