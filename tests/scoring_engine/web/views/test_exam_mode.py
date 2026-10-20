import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from scoring_engine.db import db
from scoring_engine.models.exam_credential import ExamCredential, ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.service import Service
from scoring_engine.models.team import Team
from scoring_engine.models.user import User


def test_flag_login_reuses_credential_but_awards_once(test_client, db_session):
    team = Team(name="Exam Team", color="Blue")
    db.session.add(team)
    db.session.flush()
    user = User(username="examuser", password="testpass", team=team)
    token = "exam-flag-token-value-long-enough-0123456789"
    db.session.add(user)
    db.session.flush()
    db.session.add(ExamCredential(team_id=team.id, user_id=user.id,
                                  token_hash=hashlib.sha256(token.encode()).hexdigest()))
    db.session.commit()

    invalid = test_client.post(
        "/login/flag",
        data={"flag": "incorrect-flag"},
    )
    assert invalid.status_code == 302
    assert db.session.query(ExamParticipation).filter_by(team_id=team.id).count() == 0

    for attempt in range(2):
        response = test_client.post(
            "/login/flag",
            data={"flag": token + "\n"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Your Exam Points" in response.data
        if attempt == 0:
            test_client.get("/logout")

    assert db.session.query(ExamParticipation).filter_by(team_id=team.id).count() == 1
    adjustment = db.session.query(ScoreAdjustment).filter_by(team_id=team.id).one()
    assert adjustment.points == 100
    assert adjustment.reason == "Exam participation"
    summary = test_client.get("/api/exam/summary").get_json()
    assert summary["participation_awarded"] is True
    assert summary["participation_points"] == 100
    assert summary["total"] == 100


def test_exam_stats_show_current_service_points_not_accumulated_points(blue_login, db_session):
    from tests.scoring_engine.factories import make_check, make_round
    from scoring_engine.scores import materialize_round

    client, teams = blue_login
    team = teams["blue_team"]
    service = Service(name="SSH", team=team, check_name="SSHCheck", host="exam-host", points=125)
    db.session.add(service)
    db.session.commit()

    expected = [125, 125, 0, 125]
    for number, result in enumerate((True, True, False, True), start=1):
        rnd = make_round(number=number)
        make_check(service=service, round_obj=rnd, result=result)
        materialize_round(db.session, rnd.id, rnd.number)

    response = client.get("/api/exam/stats")
    assert response.status_code == 200
    data = response.get_json()
    assert [row["points"] for row in data["rounds"]] == expected
    assert data["score"]["service"] == 125
    assert data["score"]["total"] == 125


def test_concurrent_flag_uploads_award_once(app, db_session):
    team = Team(name="Concurrent Exam Team", color="Blue")
    db.session.add(team)
    db.session.flush()
    user = User(username="concurrent-user", password="testpass", team=team)
    token = "concurrent-exam-flag-token-long-enough-0123456789"
    db.session.add(user)
    db.session.flush()
    db.session.add(ExamCredential(
        team_id=team.id,
        user_id=user.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
    ))
    db.session.commit()

    start = Barrier(2)

    def submit_flag():
        client = app.test_client()
        start.wait(timeout=5)
        return client.post(
            "/login/flag",
            data={"flag": token + "\n"},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: submit_flag(), range(2)))

    assert statuses == [302, 302]
    assert db.session.query(ExamParticipation).filter_by(team_id=team.id).count() == 1
    assert db.session.query(ScoreAdjustment).filter_by(team_id=team.id).count() == 1


def test_exam_login_uses_paste_field_and_other_users_page(test_client):
    exam_page = test_client.get("/login")
    password_page = test_client.get("/login/password")

    assert exam_page.status_code == 200
    assert b"Exam login" in exam_page.data
    assert b'type="text"' in exam_page.data
    assert b"Other users" in exam_page.data
    assert b"multipart/form-data" not in exam_page.data
    assert password_page.status_code == 200
    assert b"Username" in password_page.data
    assert b"Password" in password_page.data


def test_blue_portal_hides_scoreboard_and_service_apis(blue_login):
    client, _ = blue_login
    assert client.get("/overview").status_code == 200
    assert client.get("/stats").status_code == 200
    assert client.get("/scoreboard").status_code == 302
    assert client.get("/services").status_code == 302
    assert client.get("/api/scoreboard/get_bar_data").status_code == 404
    assert client.get("/api/overview/get_columns").status_code == 404
    assert client.get("/api/exam/summary").status_code == 200
    for path in (
        "/api/team/1/services",
        "/api/service/1/checks",
        "/api/stats",
        "/api/injects",
        "/api/flags",
        "/api/announcements/count",
    ):
        assert client.get(path).status_code == 404, path


def test_flag_provisioning_is_white_only_and_hashes_are_stored(test_client, three_teams):
    blue = three_teams["blue_team"]
    token = "exam-flag-token-value-long-enough-0123456789"
    payload = {"credentials": [{"team_id": blue.id, "token": token}]}
    response = test_client.post("/api/admin/exam/credentials", json=payload)
    assert response.status_code == 302  # anonymous login required

    test_client.post("/login", data={"username": "whiteuser", "password": "testpass"})
    response = test_client.post("/api/admin/exam/credentials", json=payload)
    assert response.status_code == 200
    credential = db.session.query(ExamCredential).filter_by(team_id=blue.id).one()
    assert credential.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert credential.token_hash != token

    repeat = test_client.post("/api/admin/exam/credentials", json=payload)
    assert repeat.status_code == 200
    rotated_token = "rotated-exam-token-value-long-enough-0123456789"
    rotated = test_client.post("/api/admin/exam/credentials", json={
        "credentials": [{"team_id": blue.id, "token": rotated_token}], "rotate": True,
    })
    assert rotated.status_code == 200
    assert db.session.query(ExamCredential).filter_by(team_id=blue.id).one().token_hash == hashlib.sha256(
        rotated_token.encode()
    ).hexdigest()
    conflict = test_client.post("/api/admin/exam/credentials", json={"credentials": [
        {"team_id": blue.id, "token": "another-exam-token-value-long-enough-012345"}
    ]})
    assert conflict.status_code == 409


def test_white_users_keep_instructor_score_views(white_login):
    client, _ = white_login
    assert client.get("/scoreboard").status_code == 200
    assert client.get("/api/scoreboard/get_bar_data").status_code == 200
    assert client.get("/api/overview/get_columns").status_code == 200
