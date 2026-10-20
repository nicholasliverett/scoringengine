from collections import defaultdict

from flask import abort, jsonify, request
from flask_login import current_user
from sqlalchemy.sql import func

from scoring_engine.cache import cache
from scoring_engine.db import db
from scoring_engine.models.check import Check
from scoring_engine.models.round import Round
from scoring_engine.models.service import Service
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team
from scoring_engine.sla import get_sla_config

from . import make_cache_key, mod


@mod.before_request
def private_exam_overview():
    if not request.path.startswith("/api/overview/"):
        return None
    if not current_user.is_authenticated or not (current_user.is_blue_team or current_user.is_white_team):
        abort(404)
    if current_user.is_blue_team and request.path != "/api/overview/get_round_data":
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


def calculate_ranks(score_dict):
    """
    Calculate ranks for a dict of {id: score} with tie handling.
    Returns dict of {id: rank} where ties get the same rank.
    """
    if not score_dict:
        return {}
    sorted_items = sorted(score_dict.items(), key=lambda x: x[1], reverse=True)
    ranks = {}
    current_rank = 1
    prev_score = None
    for i, (item_id, score) in enumerate(sorted_items):
        if prev_score is not None and score < prev_score:
            current_rank = i + 1
        ranks[item_id] = current_rank
        prev_score = score
    return ranks


@cache.memoize()
def _get_table_columns_cached(anonymize, show_both):
    """Internal cached function for table columns."""
    blue_teams = Team.get_all_blue_teams()
    team_name_map = Team.get_team_name_mapping(anonymize=anonymize, show_both=show_both)

    columns = []
    columns.append({"title": "", "data": "", "color": None})
    for team in blue_teams:
        display_name = team_name_map.get(team.id, team.name)
        columns.append({"title": display_name, "data": display_name, "color": team.rgb_color})
    return columns


@cache.memoize()
def _get_overview_data_cached(anonymize, show_both):
    """Internal cached function for overview data with host/port info."""
    data = defaultdict(lambda: defaultdict(dict))
    team_name_map = Team.get_team_name_mapping(anonymize=anonymize, show_both=show_both)

    round_obj = db.session.query(Round).order_by(Round.number.desc()).first()
    if round_obj:
        checks = (
            db.session.query(Check.service_id, Check.result)
            .join(Round)
            .filter(Round.number == round_obj.number)
            .subquery()
        )
        res = (
            db.session.query(Team.name, Service.name, Service.host, Service.port, checks.c.result)
            .join(Team)
            .filter(checks.c.service_id == Service.id)
            .all()
        )

        for r in res:
            team_name = r[0]
            display_name = team_name_map.get(team_name, team_name)
            data[display_name][r[1]] = {
                "host": r[2],
                "port": r[3],
                "passing": r[4],
            }

    # Convert defaultdict to regular dict for JSON serialization
    return {k: dict(v) for k, v in data.items()}


@mod.route("/api/overview/get_round_data")
@cache.memoize()
def overview_get_round_data():
    round_obj = db.session.query(Round).order_by(Round.number.desc()).first()
    if round_obj:
        round_start = round_obj.local_round_start
        number = round_obj.number
    else:
        round_start = ""
        number = 0
    engine_paused = Setting.get_setting("engine_paused").value
    data = {"round_start": round_start, "number": number, "engine_paused": engine_paused}
    return jsonify(data)


@mod.route("/api/overview/data")
def overview_data():
    """Get overview data with host/port info. Cached by user type."""
    anonymize, show_both = get_anonymize_mode()
    return jsonify(_get_overview_data_cached(anonymize, show_both))


@mod.route("/api/overview/get_columns")
def overview_get_columns():
    """Get table columns. Cached by user type."""
    anonymize, show_both = get_anonymize_mode()
    return jsonify(columns=_get_table_columns_cached(anonymize, show_both))


@mod.route("/api/overview/get_data")
@cache.cached(make_cache_key=make_cache_key)
def overview_get_data():
    """Get overview table data. This endpoint returns positional data matching columns.

    Every row is ``[row_label, team_1_value, team_2_value, ...]`` where the team
    values line up with the blue team columns from ``/api/overview/get_columns``.
    Values are pure data (no markup) - the front end owns the presentation:

    - ``Current Score`` / ``Current Place``: stringified integers
    - ``SLA Penalties``: penalty magnitude as an integer (0 when no penalty)
    - ``Services Up`` / ``Services Down``: this round's up/down counts as integers
    - service rows: check result booleans (or ``None`` when unchecked)
    """
    data = []
    blue_teams = db.session.query(Team).filter(Team.color == "Blue").order_by(Team.id).all()
    blue_team_ids = [team.id for team in blue_teams]
    last_round = Round.get_last_round_num()

    # Get SLA configuration
    sla_config = get_sla_config()

    current_scores = ["Current Score"]
    current_places = ["Current Place"]
    services_up = ["Services Up"]
    services_down = ["Services Down"]
    sla_penalties_row = ["SLA Penalties"]

    num_up_services = dict(
        db.session.query(Service.team_id, func.count(Service.team_id))
        .filter(Service.scoring_up.is_(True))
        .group_by(Service.team_id).all()
    )

    num_down_services = dict(
        db.session.query(Service.team_id, func.count(Service.team_id))
        .filter(Service.scoring_up.is_(False))
        .group_by(Service.team_id).all()
    )

    if len(blue_team_ids) > 0:
        # Team scores reflect the latest service state, with no round accumulation.
        from scoring_engine.scores import team_service_scores

        from . import get_effective_freeze

        _, freeze_time = get_effective_freeze()
        team_scores = team_service_scores(db.session, sla_config, freeze_time=freeze_time)
        adjusted_scores_dict = {}
        penalties_dict = {}
        for blue_team_id in blue_team_ids:
            penalties_dict[blue_team_id] = 0
            adjusted_scores_dict[blue_team_id] = team_scores.get(blue_team_id, 0)

        scores_for_ranking = team_scores

        # Calculate ranks with tie handling
        ranks_dict = calculate_ranks(scores_for_ranking)

        for blue_team_id in blue_team_ids:
            current_scores.append(str(team_scores.get(blue_team_id, 0)))
            current_places.append(str(ranks_dict.get(blue_team_id, 0)))
            # Up and down counts as separate rows so each cell holds a single
            # value -- a combined "up / down" wraps in a narrow team column and
            # makes the row height jump around.
            services_up.append(num_up_services.get(blue_team_id, 0))
            services_down.append(num_down_services.get(blue_team_id, 0))
            # Penalty magnitude as a number (0 when no penalty); the front end
            # renders it as a negative value
            sla_penalties_row.append(penalties_dict.get(blue_team_id, 0))

        data.append(current_scores)
        data.append(current_places)
        data.append(services_up)
        data.append(services_down)

        checks = (
            db.session.query(Service.id, Service.name, Check.result)
            .join(Service)
            .join(Round)
            .filter(Round.number == last_round)
            .order_by(Service.name, Service.team_id)
            .all()
        )

        service_dict = defaultdict(list)
        service_ids_dict = defaultdict(list)

        for service_id, service_name, status in checks:
            service_dict[service_name].append(status)
            service_ids_dict[service_name].append(service_id)

        # Include service IDs for white team admin links
        is_admin = current_user.is_authenticated and current_user.is_white_team
        service_ids = [] if is_admin else None

        # Loop through dictionary to create datatables formatted list
        for k, v in service_dict.items():
            data.append([k] + v)
            if is_admin:
                service_ids.append(service_ids_dict[k])

        result = {"data": data}
        if service_ids is not None:
            result["service_ids"] = service_ids
        inject_scores_visible = Setting.get_setting("inject_scores_visible")
        if inject_scores_visible and not inject_scores_visible.value:
            result["inject_scores_hidden"] = True
        return jsonify(result)
    else:
        return "{}"
