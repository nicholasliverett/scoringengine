"""Tests for private team API access under individual exam mode."""

import pytest

from scoring_engine.web.views.api.team import calculate_ranks


class TestCalculateRanks:
    def test_normal_scores(self):
        assert calculate_ranks({1: 100, 2: 90, 3: 80}) == {1: 1, 2: 2, 3: 3}

    def test_ties(self):
        assert calculate_ranks({1: 100, 2: 90, 3: 90, 4: 80}) == {1: 1, 2: 2, 3: 2, 4: 4}

    def test_all_tied(self):
        assert calculate_ranks({1: 50, 2: 50, 3: 50}) == {1: 1, 2: 1, 3: 1}

    def test_empty_dict(self):
        assert calculate_ranks({}) == {}

    def test_single_entry(self):
        assert calculate_ranks({1: 50}) == {1: 1}


class TestBlueTeamCannotAccessServiceDetails:
    @pytest.fixture(autouse=True)
    def setup(self, test_client, three_teams):
        self.client = test_client
        self.team = three_teams["blue_team"]
        self.user = three_teams["blue_user"]

    def test_requires_auth(self):
        for suffix in ("stats", "services", "services/status"):
            assert self.client.get(f"/api/team/{self.team.id}/{suffix}").status_code == 302

    def test_service_detail_apis_are_hidden(self):
        self.client.post(
            "/login",
            data={"username": self.user.username, "password": "testpass"},
            follow_redirects=True,
        )
        for suffix in ("stats", "services", "services/status"):
            response = self.client.get(f"/api/team/{self.team.id}/{suffix}")
            assert response.status_code == 404
