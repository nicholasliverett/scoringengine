"""Current-state score materialization and reads.

This module stores sparse per-round service-state snapshots:

- Write: :func:`materialize_round` (called by the engine at round close) and
  :func:`materialize_all_rounds` (backfill / non-engine contexts).
- Read: :func:`team_service_scores` (all teams, for the scoreboard/overview),
  :func:`all_team_service_scores` (all teams, backing
  ``Team.place``), :func:`team_service_score` (one team's raw total, backing
  ``Team.current_score``), and :func:`team_dynamic_service_score` (compatibility
  alias to current-state scoring). ``Team.get_array_of_scores`` /
  ``get_round_scores`` and the scoreboard line chart read these snapshots.
  These replace the full-history ``JOIN checks`` scans.

Service up/down state is cached on ``Service.scoring_up`` and repaired from the
latest completed check if rounds are rolled back.
"""

from sqlalchemy import and_, case, or_
from sqlalchemy.sql import func

from scoring_engine.models.check import Check
from scoring_engine.models.service import Service
from scoring_engine.models.service_score_snapshot import ServiceScoreSnapshot


def get_freeze_time():
    """Return the effective scoreboard freeze time as a naive-UTC datetime, or None.

    Two sources, in precedence order:

    1. The manual ``scoreboard_freeze_time`` setting (an ISO string; empty when
       not set). This is the operator's explicit last-hour freeze and wins when
       present -- it freezes the display while the engine keeps scoring.
    2. Otherwise the schedule-derived freeze: between competition windows the
       board shows the last completed window's standings (see
       :func:`scoring_engine.schedule.window_derived_freeze`).

    Normalized to naive UTC to compare against the naive-UTC datetimes the app
    stores (round_end, graded, created_at).
    """
    import pytz
    from dateutil.parser import parse

    from scoring_engine.models.setting import Setting

    setting = Setting.get_setting("scoreboard_freeze_time")
    value = setting.value if setting else None
    if value:
        try:
            dt = parse(str(value))
        except (ValueError, TypeError, OverflowError):
            dt = None
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone(pytz.utc).replace(tzinfo=None)
            return dt

    # No explicit freeze -- fall back to the competition schedule.
    from scoring_engine.schedule import window_derived_freeze

    return window_derived_freeze()


def _round_freeze_filter(query, freeze_time):
    """Restrict score snapshots to rounds closed at/before the freeze."""
    if freeze_time is None:
        return query
    from scoring_engine.models.round import Round

    return query.join(Round, Round.id == ServiceScoreSnapshot.round_id).filter(Round.round_end <= freeze_time)


def compute_round_service_points(session, round_id):
    """Return current per-team service points as of this round.

    Each service contributes its configured points while its latest completed
    check passed, and zero when it failed. Repeated passes never accumulate.
    """
    from scoring_engine.models.round import Round

    round_number = session.query(Round.number).filter(Round.id == round_id).scalar()
    if round_number is None:
        return {}
    latest = (
        session.query(Check.service_id, func.max(Check.id).label("check_id"))
        .join(Round, Check.round_id == Round.id)
        .filter(Round.number <= round_number, Check.completed.is_(True))
        .group_by(Check.service_id)
        .subquery()
    )
    rows = (
        session.query(Service.team_id, Service.points, Check.result)
        .outerjoin(latest, latest.c.service_id == Service.id)
        .outerjoin(Check, Check.id == latest.c.check_id)
        .all()
    )
    scores = {}
    for team_id, points, result in rows:
        if team_id is None:
            continue
        scores.setdefault(team_id, 0)
        if result:
            scores[team_id] += int(points or 0)
    return scores


def materialize_round(session, round_id, round_number, points_by_team=None, clear_existing=True, commit=True):
    """Write sparse service-state snapshots for teams whose score changed."""
    if clear_existing:
        session.query(ServiceScoreSnapshot).filter(
            ServiceScoreSnapshot.round_id == round_id
        ).delete(synchronize_session=False)

    if points_by_team is None:
        points_by_team = compute_round_service_points(session, round_id)

    with session.no_autoflush:
        previous_round = (
            session.query(
                ServiceScoreSnapshot.team_id,
                func.max(ServiceScoreSnapshot.round_number).label("round_number"),
            )
            .filter(ServiceScoreSnapshot.round_number < round_number)
            .group_by(ServiceScoreSnapshot.team_id)
            .subquery()
        )
        previous = dict(
            session.query(ServiceScoreSnapshot.team_id, ServiceScoreSnapshot.service_points)
            .join(
                previous_round,
                and_(
                    previous_round.c.team_id == ServiceScoreSnapshot.team_id,
                    previous_round.c.round_number == ServiceScoreSnapshot.round_number,
                ),
            )
            .all()
        )

    written = 0
    for team_id, points in points_by_team.items():
        points = int(points or 0)
        if previous.get(team_id, 0) == points:
            continue
        session.add(ServiceScoreSnapshot(
            round_id=round_id,
            round_number=round_number,
            team_id=team_id,
            service_points=points,
        ))
        written += 1
    if commit:
        session.commit()
    return written


def materialize_all_rounds(session, commit=True):
    """Rebuild service state cache and sparse score history from checks."""
    from scoring_engine.models.round import Round

    session.query(ServiceScoreSnapshot).delete(synchronize_session=False)
    services = session.query(Service).all()
    services_by_id = {service.id: service for service in services}
    states = {service.id: False for service in services}
    team_totals = {}
    prior_totals = {}
    rounds = session.query(Round.id, Round.number).order_by(Round.number, Round.id).all()
    check_iter = iter(
        session.query(Round.number, Check.service_id, Check.result)
        .join(Round, Check.round_id == Round.id)
        .filter(Check.completed.is_(True))
        .order_by(Round.number, Check.id)
        .yield_per(1000)
    )
    check_row = next(check_iter, None)
    written = 0
    for round_id, round_number in rounds:
        while check_row is not None and check_row[0] <= round_number:
            _, service_id, result = check_row
            service = services_by_id.get(service_id)
            if service is not None:
                is_up = bool(result)
                was_up = states[service_id]
                service.scoring_up = is_up
                if is_up != was_up:
                    team_totals[service.team_id] = team_totals.get(service.team_id, 0) + (
                        int(service.points or 0) if is_up else -int(service.points or 0)
                    )
                    states[service_id] = is_up
            check_row = next(check_iter, None)

        for team_id, points in team_totals.items():
            if prior_totals.get(team_id, 0) != points:
                session.add(ServiceScoreSnapshot(
                    round_id=round_id,
                    round_number=round_number,
                    team_id=team_id,
                    service_points=points,
                ))
                prior_totals[team_id] = points
                written += 1
    if commit:
        session.commit()
    return written


def team_service_scores(session, sla_config=None, freeze_time=None):
    """Return latest fixed-point service-state totals for all teams.

    Dynamic multipliers do not apply to this fork's current-state scores. A freeze
    selects the last snapshot available at that time.
    """
    query = session.query(
        ServiceScoreSnapshot.team_id,
        ServiceScoreSnapshot.round_number,
        ServiceScoreSnapshot.service_points,
    )
    query = _round_freeze_filter(query, freeze_time)
    from scoring_engine.models.team import Team

    scores = {team_id: 0 for (team_id,) in session.query(Team.id).all()}
    seen = set()
    rows = query.order_by(ServiceScoreSnapshot.round_number.desc(), ServiceScoreSnapshot.id.desc()).all()
    for team_id, _, points in rows:
        if team_id not in seen:
            scores[team_id] = int(points or 0)
            seen.add(team_id)
    return scores


def all_team_service_scores(session, freeze_time=None):
    """Return latest service-state totals, optionally frozen."""
    return team_service_scores(session, freeze_time=freeze_time)


def team_service_score(session, team_id, freeze_time=None):
    """Current service-state total for one team."""
    return int(team_service_scores(session, freeze_time=freeze_time).get(team_id, 0))


def team_adjustment_totals(session, freeze_time=None):
    """Return ``{team_id: net_adjustment_points}`` for every team with adjustments.

    Sum of the signed manual adjustments (bonuses positive, penalties negative)
    from the append-only score_adjustment log. Teams with no adjustments are absent
    (reads coalesce to zero). ``freeze_time`` restricts to adjustments created
    at/before the freeze.
    """
    from scoring_engine.models.score_adjustment import ScoreAdjustment

    query = session.query(ScoreAdjustment.team_id, func.sum(ScoreAdjustment.points))
    if freeze_time is not None:
        query = query.filter(ScoreAdjustment.created_at <= freeze_time)
    rows = query.group_by(ScoreAdjustment.team_id).all()
    return {team_id: int(total or 0) for team_id, total in rows}


def team_adjustment_total(session, team_id):
    """Net manual adjustment points for one team (0 if none)."""
    from scoring_engine.models.score_adjustment import ScoreAdjustment

    total = (
        session.query(func.coalesce(func.sum(ScoreAdjustment.points), 0))
        .filter(ScoreAdjustment.team_id == team_id)
        .scalar()
    )
    return int(total or 0)


def get_flag_point_values():
    """Return ``(user_points, root_points)`` for captured flags from settings.

    Admin-tunable mid-competition (like the SLA settings); falls back to the
    documented defaults if a setting is missing or unparseable.
    """
    from scoring_engine.models.setting import Setting

    def _int(name, default):
        setting = Setting.get_setting(name)
        try:
            return int(setting.value) if setting else default
        except (ValueError, TypeError):
            return default

    return _int("flag_points_user", 100), _int("flag_points_root", 200)


def flag_points_by_team(session, user_points=None, root_points=None, freeze_time=None):
    """Return ``{blue_team_id: captured_flag_value}`` -- each team's flag exposure.

    A captured flag is a non-dummy ``Solve`` (a blue team's agent reported a red
    flag present on one of its hosts). Each is worth ``root_points`` when the flag
    is a root flag, else ``user_points``. This is the value the red team earns for
    compromising that team; per the "add to red only" rule the blue team's own
    score is unaffected. ``freeze_time`` restricts to solves recorded at/before the
    freeze.
    """
    from scoring_engine.models.flag import Flag, Solve

    if user_points is None or root_points is None:
        cfg_user, cfg_root = get_flag_point_values()
        user_points = cfg_user if user_points is None else user_points
        root_points = cfg_root if root_points is None else root_points

    query = (
        session.query(Solve.team_id, Flag.perm, func.count())
        .join(Flag, Flag.id == Solve.flag_id)
        .filter(Flag.dummy.is_(False))
    )
    if freeze_time is not None:
        query = query.filter(Solve.created_at <= freeze_time)
    rows = query.group_by(Solve.team_id, Flag.perm).all()
    result = {}
    for team_id, perm, count in rows:
        perm_value = perm.value if hasattr(perm, "value") else perm
        points = root_points if perm_value == "root" else user_points
        result[team_id] = result.get(team_id, 0) + points * count
    return result


def red_team_flag_total(session, user_points=None, root_points=None, freeze_time=None):
    """Total red-team flag score: the sum of every blue team's captured-flag value.

    The red team scores by compromising anyone, so its total is the sum across all
    blue teams (a handful of red teams share this pool today).
    """
    return sum(flag_points_by_team(session, user_points, root_points, freeze_time=freeze_time).values())


def _service_consecutive_failures(session):
    """Return ``{service_id: consecutive_failures}`` for every service, batched.

    Consecutive failures = the run of failing *completed* checks from the most
    recent round backwards, i.e. the completed failures in rounds after the
    service's last passing round (all completed failures if it never passed).
    Equivalent to ``sla.get_consecutive_failures`` per service, in one grouped
    query instead of one-query-per-service.
    """
    last_pass = (
        session.query(Check.service_id.label("sid"), func.max(Check.round_id).label("lpr"))
        .filter(Check.completed.is_(True))
        .filter(Check.result.is_(True))
        .group_by(Check.service_id)
        .subquery()
    )
    rows = (
        session.query(Check.service_id, func.count())
        .outerjoin(last_pass, last_pass.c.sid == Check.service_id)
        .filter(Check.completed.is_(True))
        .filter(Check.result.is_(False))
        .filter(or_(last_pass.c.lpr.is_(None), Check.round_id > last_pass.c.lpr))
        .group_by(Check.service_id)
        .all()
    )
    return {sid: count for sid, count in rows}


def _service_base_scores(session, config, points_by_service, service_ids):
    """Return ``{service_id: earned base score}`` for the given services, batched.

    Only the services passed in ``service_ids`` are queried -- the caller has
    already narrowed to the handful that are actually over the penalty threshold,
    so this never scans the whole (large) checks table for the 99% of services
    that carry no penalty.

    Matches ``sla.calculate_service_base_score_with_dynamic`` exactly:

    - non-dynamic: ``count(passing checks) * points`` (``Service.score_earned``).
    - dynamic: ``sum(int(points * multiplier))`` per passing check, where the
      multiplier is the round bucket (early/mid/late). Because the multiplier is
      constant within a bucket, ``sum`` over a bucket equals
      ``count_bucket * int(points * multiplier)`` -- which reproduces the
      original per-check integer truncation without a non-portable SQL FLOOR.
    """
    if not service_ids:
        return {}

    if not config.dynamic_enabled:
        rows = (
            session.query(Check.service_id, func.count())
            .filter(Check.result.is_(True))
            .filter(Check.service_id.in_(service_ids))
            .group_by(Check.service_id)
            .all()
        )
        return {sid: count * points_by_service.get(sid, 0) for sid, count in rows}

    from scoring_engine.models.round import Round

    early, late = config.early_rounds, config.late_start_round
    # Buckets mirror the original elif chain: <= early wins first, then >= late.
    early_c = func.sum(case((Round.number <= early, 1), else_=0))
    mid_c = func.sum(case((and_(Round.number > early, Round.number < late), 1), else_=0))
    late_c = func.sum(case((and_(Round.number > early, Round.number >= late), 1), else_=0))
    rows = (
        session.query(Check.service_id, early_c, mid_c, late_c)
        .join(Round, Round.id == Check.round_id)
        .filter(Check.result.is_(True))
        .filter(Check.service_id.in_(service_ids))
        .group_by(Check.service_id)
        .all()
    )
    scores = {}
    for sid, e, m, ln in rows:
        p = points_by_service.get(sid, 0)
        # SUM() comes back as Decimal on MySQL/MariaDB (int on SQLite); coerce so
        # the later ``base * (percent / 100)`` is plain int/float arithmetic.
        e, m, ln = int(e or 0), int(m or 0), int(ln or 0)
        scores[sid] = e * int(p * config.early_multiplier) + m * p + ln * int(p * config.late_multiplier)
    return scores


def apply_consecutive_failures(session, pass_service_ids, fail_counts):
    """Fold one round's results into ``Service.consecutive_failures_cache``.

    Called by the engine at round close from the pass/fail results it already
    holds in memory -- so it never queries the round's still-pending checks (which
    would autoflush them ahead of the single atomic round commit). The caller
    wraps this in ``session.no_autoflush`` and lets the round's own commit persist
    the updates atomically with the checks and state snapshots.

    A service that passed at least once this round resets to 0; a service that
    only failed increments by its number of failing checks this round (the scan
    counts per completed check, so the streak does too).
    """
    from collections import defaultdict

    if pass_service_ids:
        session.query(Service).filter(Service.id.in_(list(pass_service_ids))).update(
            {Service.consecutive_failures_cache: 0}, synchronize_session=False
        )
    # Group fail-only services by their increment so each distinct count is a
    # single UPDATE (one UPDATE total in the common one-check-per-service case).
    by_increment = defaultdict(list)
    for service_id, count in fail_counts.items():
        if service_id in pass_service_ids:
            continue  # passed at least once -> already reset to 0
        by_increment[count].append(service_id)
    for increment, service_ids in by_increment.items():
        session.query(Service).filter(Service.id.in_(service_ids)).update(
            {Service.consecutive_failures_cache: Service.consecutive_failures_cache + increment},
            synchronize_session=False,
        )


def recompute_consecutive_failures_cache(session, service_ids=None, commit=True):
    """Recompute ``Service.consecutive_failures_cache`` from the checks table.

    The authoritative slow path, for when the incremental in-memory maintenance
    does not apply: backfill (migration / example data loaded without the engine)
    and repair after a round is deleted (engine or admin rollback). Recomputes for
    all services, or just ``service_ids``.
    """
    from collections import defaultdict

    counts = _service_consecutive_failures(session)

    target = session.query(Service.id)
    if service_ids is not None:
        target = target.filter(Service.id.in_(list(service_ids)))
    target_ids = [sid for (sid,) in target.all()]
    if not target_ids:
        if commit:
            session.commit()
        return

    # Reset the target set, then set the (few) services that have failures --
    # grouped by value so it is a handful of UPDATEs, not one per service.
    session.query(Service).filter(Service.id.in_(target_ids)).update(
        {Service.consecutive_failures_cache: 0}, synchronize_session=False
    )
    target_set = set(target_ids)
    by_value = defaultdict(list)
    for service_id, failures in counts.items():
        if failures and service_id in target_set:
            by_value[failures].append(service_id)
    for value, sids in by_value.items():
        session.query(Service).filter(Service.id.in_(sids)).update(
            {Service.consecutive_failures_cache: value}, synchronize_session=False
        )
    if commit:
        session.commit()


def recompute_service_scoring_states(session, service_ids=None, commit=True):
    """Repair cached up/down state from each service's latest completed check."""
    latest = (
        session.query(Check.service_id, func.max(Check.id).label("check_id"))
        .filter(Check.completed.is_(True))
    )
    if service_ids is not None:
        service_ids = list(service_ids)
        if not service_ids:
            return
        latest = latest.filter(Check.service_id.in_(service_ids))
    latest = latest.group_by(Check.service_id).subquery()
    states = dict(
        session.query(Check.service_id, Check.result)
        .join(latest, latest.c.check_id == Check.id)
        .all()
    )
    services = session.query(Service)
    if service_ids is not None:
        services = services.filter(Service.id.in_(service_ids))
    for service in services.all():
        service.scoring_up = bool(states.get(service.id, False))
    if commit:
        session.commit()


def team_penalties(session, config):
    """Return ``{team_id: total_sla_penalty}`` for every team, batched.

    Replaces the per-service ``sla.calculate_team_total_penalties`` loop -- which
    issued two check queries for *every* service (~20k queries at 100 teams x 100
    services) -- with a read that scans no checks at all for the failure counts:
    they come from the materialized ``Service.consecutive_failures_cache`` (kept
    current by the engine at round close). Produces byte-identical penalties
    (guarded by an equivalence test); teams with no penalty are absent.

    The (small) earned-score scan then runs only for the services actually over
    the penalty threshold -- typically a tiny fraction -- so a service with no
    penalty never has its base score queried.

    Deliberately not freeze-aware, matching the existing penalty path (the frozen
    public scoreboard already shows live penalties).
    """
    if not config.sla_enabled:
        return {}

    from scoring_engine.sla import calculate_sla_penalty_percent

    percents = {}
    points_by_service = {}
    team_by_service = {}
    for service_id, team_id, points, failures in session.query(
        Service.id, Service.team_id, Service.points, Service.consecutive_failures_cache
    ).all():
        percent = calculate_sla_penalty_percent(failures or 0, config)
        if percent > 0:
            percents[service_id] = percent
            points_by_service[service_id] = points
            team_by_service[service_id] = team_id
    if not percents:
        return {}

    penalized_ids = list(percents)
    base_scores = _service_base_scores(session, config, points_by_service, penalized_ids)

    penalties = {}
    for service_id, percent in percents.items():
        penalty = int(base_scores.get(service_id, 0) * (percent / 100))
        if penalty:
            team_id = team_by_service[service_id]
            penalties[team_id] = penalties.get(team_id, 0) + penalty
    return penalties


def team_dynamic_service_score(session, team_id, sla_config):
    """Compatibility alias: current-state scores are never dynamically multiplied."""
    return team_service_score(session, team_id)
