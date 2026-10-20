"""Cache-key isolation coverage.

The web tier serves cached JSON whose visibility depends on the viewer's role
and team -- see ``scoring_engine/web/views/api/__init__.py:make_cache_key``. A
regression in that key (dropping the team id, an inverted role check, an early
return) would let one team read another team's cached response, exactly the
"data visibility varies by team role and ID" risk CLAUDE.md warns about.

The rest of the suite runs with ``cache_type = null`` (NullCache), so nothing
otherwise exercises this path: the key is computed but never stored, so a
cross-viewer leak can never happen in a test. These tests

  1. unit-test ``make_cache_key`` for every viewer type, and
  2. stand up a real in-process ``SimpleCache`` and prove one viewer cannot be
     served another viewer's cached payload end-to-end.

``TestCrossViewerCacheIsolation`` includes ``test_cache_backend_is_actually_serving``
so the isolation assertions can never pass vacuously against a Null cache.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from flask import g

from scoring_engine.db import db
from scoring_engine.models.check import Check
from scoring_engine.models.round import Round
from scoring_engine.models.service import Service
from scoring_engine.models.team import Team
from scoring_engine.models.user import User
from scoring_engine.web.views.api import make_cache_key


def _login(client, username, password="testpass"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


# Lightweight stand-ins for ``g.user`` -- make_cache_key only reads is_anonymous,
# is_white_team, is_red_team and team.id.
def _anon():
    return SimpleNamespace(is_anonymous=True)


def _white():
    return SimpleNamespace(is_anonymous=False, is_white_team=True)


def _red():
    return SimpleNamespace(is_anonymous=False, is_white_team=False, is_red_team=True)


def _blue(team_id):
    return SimpleNamespace(
        is_anonymous=False,
        is_white_team=False,
        is_red_team=False,
        team=SimpleNamespace(id=team_id),
    )


class TestMakeCacheKey:
    """Unit tests for the cache-key function in isolation."""

    def _key(self, app, path, user):
        with app.test_request_context(path):
            g.user = user
            return make_cache_key()

    def test_key_per_viewer_type(self, app):
        path = "/api/overview/get_data"
        assert self._key(app, path, _anon()) == f"{path}_anonymous"
        assert self._key(app, path, _white()) == f"{path}_white"
        assert self._key(app, path, _red()) == f"{path}_red"
        assert self._key(app, path, _blue(5)) == f"{path}_team_5"

    def test_all_viewer_keys_are_distinct(self, app):
        path = "/api/overview/get_data"
        keys = [
            self._key(app, path, _anon()),
            self._key(app, path, _white()),
            self._key(app, path, _red()),
            self._key(app, path, _blue(5)),
            self._key(app, path, _blue(7)),
        ]
        assert len(set(keys)) == len(keys), keys

    def test_two_blue_teams_get_distinct_keys(self, app):
        path = "/api/team/1/services"
        assert self._key(app, path, _blue(5)) != self._key(app, path, _blue(7))

    def test_key_includes_request_path(self, app):
        user = _blue(5)
        assert self._key(app, "/api/a", user) != self._key(app, "/api/b", user)
        assert self._key(app, "/api/a", user).startswith("/api/a")

    def test_real_user_models_flow_through(self, app, three_teams):
        # Guard the real User role properties, not just the fakes above.
        with app.test_request_context("/api/overview/get_data"):
            g.user = three_teams["white_user"]
            assert make_cache_key().endswith("_white")
            g.user = three_teams["red_user"]
            assert make_cache_key().endswith("_red")
            g.user = three_teams["blue_user"]
            assert make_cache_key().endswith(f"_team_{three_teams['blue_team'].id}")


@pytest.fixture()
def simple_cache(app):
    """Swap the app's (normally Null) cache backend for an in-process SimpleCache
    so the make_cache_key path is actually stored/served, then restore it.

    The ``app`` fixture is session-scoped, so restoring the original backend on
    teardown is what keeps other tests on their expected NullCache.
    """
    from flask_caching.backends.simplecache import SimpleCache

    from scoring_engine.cache import cache as cache_obj

    ext = app.extensions["cache"]
    original = ext[cache_obj]
    ext[cache_obj] = SimpleCache()
    try:
        yield ext[cache_obj]
    finally:
        ext[cache_obj] = original


class TestCrossViewerCacheIsolation:
    """End-to-end: with a live cache, one viewer never gets another's payload."""

    @pytest.fixture(autouse=True)
    def setup(self, app, three_teams, simple_cache):
        self.app = app
        self.teams = three_teams
        self.blue_team1 = three_teams["blue_team"]

        # A second blue team + user, so we have two mutually-unauthorized teams.
        self.blue_team2 = Team(name="Blue Team 2", color="Blue")
        db.session.add(self.blue_team2)
        db.session.flush()
        self.blue_user2 = User(username="blueuser2", password="testpass", team=self.blue_team2)
        db.session.add(self.blue_user2)

        # One service + check for team 1, so its /services endpoint returns a body.
        self.svc1 = Service(
            name="HTTP",
            check_name="HTTPCheck",
            host="10.0.0.1",
            port=80,
            team=self.blue_team1,
            points=100,
        )
        db.session.add(self.svc1)
        db.session.flush()
        rnd = Round(number=1)
        db.session.add(rnd)
        db.session.add(
            Check(
                round=rnd,
                service=self.svc1,
                result=True,
                output="ok",
                reason="",
                command="noop",
                completed=True,
                completed_timestamp=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    # ------------------------------------------------------------------
    # Per-request identity helpers.
    #
    # The db_session fixture holds a single app context for the whole test, and
    # the test client reuses it. Flask-Login caches the resolved user on that
    # context's ``g._login_user`` (and the login view short-circuits when it
    # sees an already-authenticated user), so without intervention the first
    # client's identity leaks into every later client. Production never sees
    # this -- each real request gets its own app context and resolves identity
    # from its own cookie. Clearing the cache before each interaction reproduces
    # that, so the cache keys under test are the ones production would compute.
    # ------------------------------------------------------------------
    def _login_client(self, username):
        client = self.app.test_client()
        g.pop("_login_user", None)
        _login(client, username)
        return client

    def _get(self, client, path):
        g.pop("_login_user", None)
        return client.get(path)

    def test_blue_service_data_is_hidden(self):
        """Blue users cannot fetch service details, regardless of cache state."""
        path = f"/api/team/{self.blue_team1.id}/services"
        owner = self._login_client("blueuser")
        assert self._get(owner, path).status_code == 404

    def test_team_cannot_read_another_teams_cached_services(self):
        """Team 2 requesting team 1's URL must be denied, never handed team 1's
        cached body. The viewer's team id is in the cache key; if it weren't,
        team 2 would hit team 1's primed 200."""
        path = f"/api/team/{self.blue_team1.id}/services"

        owner = self._login_client("blueuser")
        r_owner = self._get(owner, path)
        assert r_owner.status_code == 404

        peer = self._login_client("blueuser2")
        r_peer = self._get(peer, path)
        assert r_peer.status_code == 404

    def test_blue_team_does_not_receive_white_admin_cache(self):
        """/api/overview/get_data carries admin-only ``service_ids`` for white.
        A blue viewer must not be served white's primed payload."""
        path = "/api/overview/get_data"

        white = self._login_client("whiteuser")
        r_white = self._get(white, path)
        assert r_white.status_code == 200
        assert "service_ids" in r_white.get_json(), "white payload should carry service_ids"

        blue = self._login_client("blueuser")
        r_blue = self._get(blue, path)
        assert r_blue.status_code == 404

    def test_anonymous_does_not_receive_white_admin_cache(self):
        """The public/anonymous viewer must not be served white's cached payload."""
        path = "/api/overview/get_data"

        white = self._login_client("whiteuser")
        assert "service_ids" in self._get(white, path).get_json()

        anon = self.app.test_client()
        r_anon = self._get(anon, path)
        assert r_anon.status_code == 404
