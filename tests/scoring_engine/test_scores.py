"""Tests for current-state service scoring snapshots."""

from scoring_engine.db import db
from scoring_engine.models.service_score_snapshot import ServiceScoreSnapshot
from scoring_engine.models.service import Service
from scoring_engine.scores import (
    compute_round_service_points,
    materialize_all_rounds,
    materialize_round,
    team_service_score,
)
from tests.scoring_engine.factories import make_check, make_round, make_service, make_team


def test_repeated_passes_do_not_accumulate_and_failure_sets_score_to_zero():
    team = make_team(color="Blue")
    service = make_service(team=team, name="SSH")
    service.points = 100
    db.session.commit()

    rounds = [make_round(number=n) for n in range(1, 4)]
    make_check(service=service, round_obj=rounds[0], result=True)
    assert compute_round_service_points(db.session, rounds[0].id) == {team.id: 100}
    materialize_round(db.session, rounds[0].id, rounds[0].number)

    make_check(service=service, round_obj=rounds[1], result=True)
    assert compute_round_service_points(db.session, rounds[1].id) == {team.id: 100}
    assert materialize_round(db.session, rounds[1].id, rounds[1].number) == 0

    make_check(service=service, round_obj=rounds[2], result=False)
    assert compute_round_service_points(db.session, rounds[2].id) == {team.id: 0}
    assert materialize_round(db.session, rounds[2].id, rounds[2].number) == 1
    assert team_service_score(db.session, team.id) == 0
    assert [r.service_points for r in db.session.query(ServiceScoreSnapshot).order_by(ServiceScoreSnapshot.round_number)] == [100, 0]


def test_pass_recovery_restores_fixed_points_once():
    team = make_team(color="Blue")
    service = make_service(team=team)
    service.points = 75
    db.session.commit()
    rounds = [make_round(number=n) for n in range(1, 4)]
    for rnd, result in zip(rounds, (True, False, True)):
        make_check(service=service, round_obj=rnd, result=result)
        materialize_round(db.session, rnd.id, rnd.number)
    assert team_service_score(db.session, team.id) == 75
    assert [r.service_points for r in db.session.query(ServiceScoreSnapshot).order_by(ServiceScoreSnapshot.round_number)] == [75, 0, 75]


def test_materialize_all_rounds_rebuilds_sparse_history_and_live_state():
    team = make_team(color="Blue")
    service_a = make_service(team=team, name="SSH")
    service_b = make_service(team=team, name="Web")
    service_a.points = 100
    service_b.points = 50
    db.session.commit()
    rounds = [make_round(number=n) for n in range(1, 4)]
    make_check(service=service_a, round_obj=rounds[0], result=True)
    make_check(service=service_b, round_obj=rounds[0], result=True)
    make_check(service=service_a, round_obj=rounds[1], result=False)
    make_check(service=service_b, round_obj=rounds[1], result=True)
    make_check(service=service_a, round_obj=rounds[2], result=False)
    make_check(service=service_b, round_obj=rounds[2], result=True)

    materialize_all_rounds(db.session)
    assert team_service_score(db.session, team.id) == 50
    assert service_a.scoring_up is False
    assert service_b.scoring_up is True
    assert [team.get_round_scores(n) for n in (1, 2, 3)] == [150, 50, 50]


def test_precomputed_points_match_queried_path_and_commit_false_rolls_back():
    team = make_team(color="Blue")
    service = make_service(team=team)
    service.points = 100
    db.session.commit()
    rnd = make_round(number=1)
    make_check(service=service, round_obj=rnd, result=True)
    points = compute_round_service_points(db.session, rnd.id)
    materialize_round(db.session, rnd.id, rnd.number, points_by_team=points, clear_existing=False, commit=False)
    assert db.session.query(ServiceScoreSnapshot).filter_by(round_id=rnd.id).count() == 1
    db.session.rollback()
    assert db.session.query(ServiceScoreSnapshot).filter_by(round_id=rnd.id).count() == 0


def test_team_without_a_passing_service_has_zero_score():
    team = make_team(color="Blue")
    service = make_service(team=team)
    db.session.commit()
    rnd = make_round(number=1)
    make_check(service=service, round_obj=rnd, result=False)
    assert compute_round_service_points(db.session, rnd.id) == {team.id: 0}
    assert materialize_round(db.session, rnd.id, rnd.number) == 0
    assert team_service_score(db.session, team.id) == 0
