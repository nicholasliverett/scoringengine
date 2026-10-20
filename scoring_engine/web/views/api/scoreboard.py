from collections import defaultdict
from datetime import datetime, timezone

from flask import abort, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.sql import func

from scoring_engine.cache import cache
from scoring_engine.db import db
from scoring_engine.models.inject import Inject, InjectRubricScore
from scoring_engine.models.round import Round
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team
from scoring_engine.sla import get_sla_config

from . import mod


@mod.before_request
def white_team_only():
    # The scoreboard and its JSON endpoints are private in the exam deployment.
    if request.path.startswith("/api/scoreboard") and (
        not current_user.is_authenticated or not current_user.is_white_team
    ):
        abort(404)


def get_anonymize_mode():
    """
    Determine how team names should be displayed.
    Returns: (anonymize, show_both) tuple
        - (True, False): Show only anonymous names (public/blue teams)
        - (False, True): Show "RealName (Anonymous)" (white team when enabled)
        - (False, False): Show only real names (setting disabled)
    """
    setting = Setting.get_setting("anonymize_team_names")
    anonymize_enabled = setting.value is True if setting else False

    if not anonymize_enabled:
        return (False, False)

    # White team sees both names when anonymization is enabled
    if current_user.is_authenticated and current_user.is_white_team:
        return (False, True)

    # Everyone else sees only anonymous names
    return (True, False)


def calculate_team_scores_with_dynamic_scoring(sla_config, freeze_time=None):
    """
    Calculate team scores with dynamic scoring multipliers applied per-round.

    Returns dict mapping team_id to total score with multipliers applied.

    Reads the latest sparse service-state snapshots rather than re-summing the
    full check history on every scoreboard render.
    ``freeze_time`` restricts to rounds closed at/before the wall-clock freeze.
    """
    from scoring_engine.scores import team_service_scores

    return team_service_scores(db.session, sla_config, freeze_time=freeze_time)


@cache.memoize()
def _get_bar_data_cached(anonymize, show_both, frozen_view=False):
    """
    Internal cached function for bar chart data.
    Cache key includes anonymize/show_both/frozen_view flags for separate caches
    per user type (frozen_view distinguishes the white/live view from the frozen
    public view, which anonymize/show_both alone do not when anonymization is off).
    """
    from scoring_engine.scores import get_freeze_time, team_adjustment_totals

    freeze_time = get_freeze_time() if frozen_view else None

    sla_config = get_sla_config()
    current_scores = calculate_team_scores_with_dynamic_scoring(sla_config, freeze_time=freeze_time)
    adjustments = team_adjustment_totals(db.session, freeze_time=freeze_time)

    inject_scores_visible = Setting.get_setting("inject_scores_visible")
    if inject_scores_visible and inject_scores_visible.value:
        inject_query = (
            db.session.query(Inject.team_id, func.sum(InjectRubricScore.score))
            .join(InjectRubricScore)
            .filter(Inject.status == "Graded")
        )
        if freeze_time is not None:
            inject_query = inject_query.filter(Inject.graded <= freeze_time)
        inject_scores = dict(inject_query.group_by(Inject.team_id).all())
    else:
        inject_scores = {}

    team_data = {}
    team_labels = []
    team_scores = []
    team_inject_scores = []
    team_sla_penalties = []
    team_adjustments = []
    team_adjusted_scores = []
    # Pre-weight values, kept alongside the weighted ones so the UI can show the
    # breakdown (e.g. "120 service x 1.5"). Equal to the weighted values when
    # weighted scoring is off.
    team_raw_service_scores = []
    team_raw_inject_scores = []

    team_colors = []

    # Current-state service points are fixed and are not weighted. Keep the
    # existing optional inject weighting for deployments that still use injects.
    weighted = sla_config.weighted_scoring_enabled

    blue_teams = db.session.query(Team).filter(Team.color == "Blue").order_by(Team.id).all()
    team_name_map = Team.get_team_name_mapping(anonymize=anonymize, show_both=show_both)

    for blue_team in blue_teams:
        display_name = team_name_map.get(blue_team.id, blue_team.name)
        team_labels.append(display_name)
        team_colors.append(blue_team.rgb_color)
        service_score = current_scores.get(blue_team.id, 0)
        inject_score = inject_scores.get(blue_team.id, 0)
        adjustment = adjustments.get(blue_team.id, 0)

        team_raw_service_scores.append(str(service_score))
        team_raw_inject_scores.append(str(inject_score))
        weighted_service = service_score
        weighted_inject = int(round(inject_score * sla_config.inject_weight)) if weighted else inject_score
        team_scores.append(str(weighted_service))
        team_inject_scores.append(str(weighted_inject))
        team_adjustments.append(str(adjustment))

        total_base_score = weighted_service + weighted_inject + adjustment
        team_sla_penalties.append("0")
        team_adjusted_scores.append(str(total_base_score))

    team_data["labels"] = team_labels
    team_data["colors"] = team_colors
    team_data["service_scores"] = team_scores
    team_data["inject_scores"] = team_inject_scores
    team_data["sla_penalties"] = team_sla_penalties
    team_data["adjustments"] = team_adjustments
    team_data["adjusted_scores"] = team_adjusted_scores
    team_data["sla_enabled"] = False
    team_data["weighted_scoring_enabled"] = weighted
    if weighted:
        team_data["weights"] = {
            "service": 1.0,
            "inject": sla_config.inject_weight,
            "flag": sla_config.flag_weight,
        }
        team_data["raw_service_scores"] = team_raw_service_scores
        team_data["raw_inject_scores"] = team_raw_inject_scores
    return team_data


# Cap on the number of x-points in a line series. One point per round makes the
# response grow without bound (~2.4 MB at 100 teams x 3000 rounds); this keeps it
# flat regardless of competition length. A few hundred points is well past what a
# line chart can resolve on screen.
LINE_CHART_MAX_POINTS = 400


def _downsample_indices(n, max_points):
    """Evenly-spaced indices into ``range(n)``, at most ``max_points``, always
    including the first and last. Returns every index when ``n <= max_points``.

    Uniform sampling is enough here because the cumulative score series is smooth
    and monotonic; it also gives every team the same x-points, which a shared
    rounds axis requires.
    """
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / (max_points - 1)
    indices = sorted({round(i * step) for i in range(max_points)})
    if indices[-1] != n - 1:
        indices.append(n - 1)
    return indices


@cache.memoize()
def _get_line_data_cached(anonymize, show_both, frozen_view=False):
    """
    Internal cached function for line chart data.
    Cache key includes anonymize/show_both/frozen_view flags for separate caches
    per user type.
    """
    from scoring_engine.scores import get_freeze_time

    freeze_time = get_freeze_time() if frozen_view else None

    last_round = Round.get_last_round_num()
    sla_config = get_sla_config()

    blue_teams = (
        db.session.query(Team.id, Team.name, Team.rgb_color).filter(Team.color == "Blue").order_by(Team.id).all()
    )

    # Per-team current-state snapshots (no full-history scan; round number is
    # denormalized onto each snapshot).
    from scoring_engine.models.service_score_snapshot import ServiceScoreSnapshot

    round_query = db.session.query(
        ServiceScoreSnapshot.team_id,
        ServiceScoreSnapshot.round_number,
        ServiceScoreSnapshot.service_points,
    )
    if freeze_time is not None:
        round_query = round_query.join(
            Round, Round.id == ServiceScoreSnapshot.round_id
        ).filter(Round.round_end <= freeze_time)
    round_scores = round_query.order_by(
        ServiceScoreSnapshot.team_id, ServiceScoreSnapshot.round_number
    ).all()

    scores_dict = defaultdict(lambda: defaultdict(int))
    for team_id, round_number, round_score in round_scores:
        scores_dict[team_id][round_number] = int(round_score or 0)

    # x-axis is every round (0..last_round), downsampled to a bounded set of
    # points shared by all teams.
    n = last_round + 1
    sampled = _downsample_indices(n, LINE_CHART_MAX_POINTS)

    team_data = {
        "team": [],
        "rounds": [f"Round {i}" for i in sampled],
    }

    team_name_map = Team.get_team_name_mapping(anonymize=anonymize, show_both=show_both)

    for team_id, team_name, rgb_color in blue_teams:
        display_name = team_name_map.get(team_id, team_name)
        team_rounds = scores_dict[team_id]
        # Sparse snapshots carry forward until that team's current service state
        # changes again. Scores may rise or fall; they are not cumulative.
        timeline = []
        current = 0
        for r in range(n):
            current = team_rounds.get(r, current)
            timeline.append(current)
        team_data["team"].append(
            {
                "name": display_name,
                "scores": [timeline[i] for i in sampled],
                "color": rgb_color,
            }
        )

    return team_data


@mod.route("/api/scoreboard/freeze_status")
def scoreboard_freeze_status():
    """Report whether the scoreboard is frozen, for the banner + countdown.

    Returns the freeze instant and the current server time as epoch seconds so the
    client can render a countdown that does not depend on the viewer's clock being
    correct -- the whole point of a countdown across timezones. Public: the banner
    shows to everyone; only the white team's data stays live (``white_live``).
    """
    import pytz

    from scoring_engine.config import config
    from scoring_engine.datetime_utils import ensure_utc_aware
    from scoring_engine.scores import get_freeze_time

    freeze_time = get_freeze_time()
    if freeze_time is None:
        return jsonify(frozen=False)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_white = current_user.is_authenticated and current_user.is_white_team
    display = ensure_utc_aware(freeze_time).astimezone(pytz.timezone(config.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    return jsonify(
        frozen=True,
        white_live=is_white,
        freeze_epoch=int(ensure_utc_aware(freeze_time).timestamp()),
        server_epoch=int(ensure_utc_aware(now).timestamp()),
        freeze_display=display,
    )


@mod.route("/api/scoreboard/get_bar_data")
def scoreboard_get_bar_data():
    """Get bar chart data. Cached separately by user type and freeze view."""
    from . import get_effective_freeze

    anonymize, show_both = get_anonymize_mode()
    frozen_view, _ = get_effective_freeze()
    return jsonify(_get_bar_data_cached(anonymize, show_both, frozen_view))


@mod.route("/api/scoreboard/get_line_data")
def scoreboard_get_line_data():
    """Get line chart data. Cached separately by user type and freeze view."""
    from . import get_effective_freeze

    anonymize, show_both = get_anonymize_mode()
    frozen_view, _ = get_effective_freeze()
    return jsonify(_get_line_data_cached(anonymize, show_both, frozen_view))
