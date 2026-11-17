"""Tests for the wall-clock scoreboard freeze (wave 2, phase 6)."""

from datetime import datetime

import pytest

from scoring_engine.db import db
from scoring_engine.models.flag import Flag, FlagTypeEnum, Perm, Platform, Solve
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.setting import Setting
from scoring_engine.scores import (
    all_team_service_scores,
    flag_points_by_team,
    get_freeze_time,
    materialize_all_rounds,
    team_adjustment_totals,
)
from tests.scoring_engine.factories import make_check, make_round, make_service, make_team

# A fixed timeline: round 1 closes at T1, round 2 at T2; freeze falls between them.
T1 = datetime(2026, 1, 1, 10, 0, 0)
T2 = datetime(2026, 1, 1, 11, 0, 0)
FREEZE = datetime(2026, 1, 1, 10, 30, 0)


def _set_freeze(value):
    setting = Setting.get_setting("scoreboard_freeze_time")
    setting.value = value
    db.session.commit()
    Setting.clear_cache("scoreboard_freeze_time")


class TestGetFreezeTime:
    def test_empty_setting_is_not_frozen(self):
        _set_freeze("")
        assert get_freeze_time() is None

    def test_iso_value_parses_to_naive_utc(self):
        _set_freeze("2026-01-01T10:30:00")
        assert get_freeze_time() == FREEZE

    def test_tz_aware_value_normalized_to_utc(self):
        _set_freeze("2026-01-01T05:30:00-05:00")  # == 10:30 UTC
        assert get_freeze_time() == FREEZE

    def test_garbage_value_is_not_frozen(self):
        _set_freeze("not a date")
        assert get_freeze_time() is None


class TestGetFreezeTimeSchedule:
    """get_freeze_time composes the manual freeze with the competition schedule."""

    def _past_window(self):
        # A window entirely in the past, so "now" is always after it and it
        # deterministically implies a freeze at its close.
        from scoring_engine.models.competition_window import CompetitionWindow

        end = datetime(2020, 1, 1, 17, 0, 0)
        db.session.add(
            CompetitionWindow(name="past", start_time=datetime(2020, 1, 1, 9, 0, 0), end_time=end, enabled=True)
        )
        db.session.commit()
        return end

    def test_manual_freeze_overrides_schedule(self):
        self._past_window()
        _set_freeze("2026-01-01T10:30:00")
        assert get_freeze_time() == FREEZE

    def test_schedule_derived_when_no_manual_freeze(self):
        end = self._past_window()
        _set_freeze("")
        assert get_freeze_time() == end

    def test_no_manual_no_windows_is_not_frozen(self):
        _set_freeze("")
        assert get_freeze_time() is None


class TestServiceScoreFreeze:
    def test_only_rounds_closed_before_freeze_count(self):
        team = make_team(color="Blue")
        svc = make_service(team=team)
        svc.points = 100
        db.session.commit()
        r1 = make_round(number=1, round_end=T1)
        r2 = make_round(number=2, round_end=T2)
        make_check(service=svc, round_obj=r1, result=True)
        make_check(service=svc, round_obj=r2, result=True)
        materialize_all_rounds(db.session)

        assert all_team_service_scores(db.session) == {team.id: 100}  # live current state
        assert all_team_service_scores(db.session, freeze_time=FREEZE) == {team.id: 100}  # frozen

    def test_round_still_open_is_excluded_by_freeze(self):
        team = make_team(color="Blue")
        svc = make_service(team=team)
        svc.points = 100
        db.session.commit()
        r1 = make_round(number=1, round_end=T1)
        r_open = make_round(number=2, round_end=None)  # in progress: no round_end
        make_check(service=svc, round_obj=r1, result=True)
        make_check(service=svc, round_obj=r_open, result=True)
        materialize_all_rounds(db.session)

        # Only the closed round counts under a freeze.
        assert all_team_service_scores(db.session, freeze_time=FREEZE) == {team.id: 100}


class TestAdjustmentFreeze:
    def test_only_adjustments_before_freeze_count(self):
        team = make_team(color="Blue")
        db.session.commit()
        db.session.add_all(
            [
                ScoreAdjustment(team_id=team.id, points=100, reason="early", created_at=T1),
                ScoreAdjustment(team_id=team.id, points=50, reason="late", created_at=T2),
            ]
        )
        db.session.commit()
        assert team_adjustment_totals(db.session) == {team.id: 150}
        assert team_adjustment_totals(db.session, freeze_time=FREEZE) == {team.id: 100}


class TestFlagFreeze:
    def test_only_solves_before_freeze_count(self):
        team = make_team(color="Blue")
        db.session.commit()
        flag = Flag(
            type=FlagTypeEnum.file,
            platform=Platform.nix,
            perm=Perm.user,
            data={"path": "/f", "content": "x"},
            start_time=T1,
            end_time=T2,
            dummy=False,
        )
        db.session.add(flag)
        db.session.commit()
        db.session.add_all(
            [
                Solve(host="h1", team=team, flag=flag, created_at=T1),
                Solve(host="h2", team=team, flag=flag, created_at=T2),
            ]
        )
        db.session.commit()
        assert flag_points_by_team(db.session) == {team.id: 200}  # both, 100 each
        assert flag_points_by_team(db.session, freeze_time=FREEZE) == {team.id: 100}  # only the early one


class TestFreezeEndpoints:
    @pytest.fixture(autouse=True)
    def setup(self, white_login):
        self.client, self.teams = white_login

    def test_status_not_frozen_by_default(self):
        assert self.client.get("/api/scoreboard/freeze_status").json == {"frozen": False}

    def test_admin_set_and_status(self):
        resp = self.client.post("/api/admin/freeze", json={"freeze_time": "2026-01-01T10:30:00"})
        assert resp.status_code == 200
        status = self.client.get("/api/scoreboard/freeze_status").json
        assert status["frozen"] is True
        assert status["white_live"] is True  # setup logged in as white
        assert "freeze_epoch" in status and "server_epoch" in status

    def test_admin_clear(self):
        self.client.post("/api/admin/freeze", json={"freeze_time": "2026-01-01T10:30:00"})
        self.client.post("/api/admin/freeze", json={"freeze_time": ""})
        assert self.client.get("/api/scoreboard/freeze_status").json == {"frozen": False}

    def test_set_requires_white_team(self):
        self.client.get("/logout")
        from tests.scoring_engine.conftest import _login

        _login(self.client, "blueuser")
        resp = self.client.post("/api/admin/freeze", json={"freeze_time": "2026-01-01T10:30:00"})
        assert resp.status_code == 404


class TestScoreboardFreezeView:
    def test_white_sees_live_others_see_frozen(self, test_client, three_teams):
        blue = three_teams["blue_team"]
        svc = make_service(team=blue)
        svc.points = 100
        db.session.commit()
        r1 = make_round(number=1, round_end=T1)
        r2 = make_round(number=2, round_end=T2)
        make_check(service=svc, round_obj=r1, result=True)
        make_check(service=svc, round_obj=r2, result=True)
        materialize_all_rounds(db.session)
        _set_freeze("2026-01-01T10:30:00")

        from tests.scoring_engine.conftest import _login

        # White: live (the service contributes its 100 points once)
        _login(test_client, "whiteuser")
        white_data = test_client.get("/api/scoreboard/get_bar_data").json
        wi = white_data["labels"].index("Blue Team")
        assert white_data["service_scores"][wi] == "100"

        # Blue competitors have no access to the public scoreboard.
        test_client.get("/logout")
        _login(test_client, "blueuser")
        assert test_client.get("/api/scoreboard/get_bar_data").status_code == 404
