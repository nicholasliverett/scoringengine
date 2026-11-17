from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, desc, func
from sqlalchemy.orm import relationship

from scoring_engine.db import db
from scoring_engine.models.base import Base
from scoring_engine.models.check import Check


def _get_rank_from_scores(scores, target_id):
    """
    Get rank for target_id from list of (id, score) tuples.
    Handles ties: same score = same rank.
    Returns None if target_id not in scores.
    """
    if not scores or target_id not in [s[0] for s in scores]:
        return None

    # scores are already sorted descending by the query
    current_rank = 1
    prev_score = None
    for i, (item_id, score) in enumerate(scores):
        if prev_score is not None and score < prev_score:
            current_rank = i + 1
        if item_id == target_id:
            return current_rank
        prev_score = score
    return None


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    # Blue-team exam overview label: service name, Finding N, or Threat N.
    exam_display = Column(String(16), nullable=False, default="finding", server_default="finding")
    check_name = Column(String(100), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"))
    team = relationship("Team", back_populates="services", lazy="joined")
    checks = relationship("Check", back_populates="service")
    accounts = relationship("Account", back_populates="service")
    points = Column(Integer, default=100)
    environments = relationship("Environment", back_populates="service")
    host = Column(String(256), nullable=False)
    port = Column(Integer, default=0)
    worker_queue = Column(String(50), default="main")
    # Latest completed check state, updated atomically with each closed round.
    # Service points are now a live state score: up earns `points`, down earns 0.
    scoring_up = Column(Boolean, nullable=False, default=False, server_default="0")
    # Materialized consecutive-failure count, maintained by the engine at round
    # close (scores.apply_consecutive_failures). Read by the batched SLA penalty
    # path (scores.team_penalties) so the scoreboard never scans the whole checks
    # table. Kept in sync with the ``consecutive_failures`` property, which
    # recomputes the same value from checks and stays the source of truth for the
    # single-service paths and the equivalence test.
    consecutive_failures_cache = Column(Integer, nullable=False, default=0, server_default="0")

    def check_result_for_round(self, round_num):
        """
        Get check result for a specific round.
        Optimized to use a DB query instead of loading all checks into memory.
        """
        from scoring_engine.models.round import Round

        check = (
            db.session.query(Check)
            .join(Round)
            .filter(Check.service_id == self.id)
            .filter(Round.number == round_num)
            .first()
        )
        if check:
            return check.result
        return False

    def last_check_result(self):
        """
        Get the result of the most recent check for this service.
        Uses a DB query to avoid session/lazy loading issues.
        """
        last_check = (
            db.session.query(Check)
            .filter(Check.service_id == self.id, Check.completed.is_(True))
            .order_by(desc(Check.id))
            .first()
        )
        if last_check:
            return last_check.result
        return None

    @property
    def checks_reversed(self):
        return db.session.query(Check).filter(Check.service_id == self.id).order_by(desc(Check.round_id)).all()

    @property
    def rank(self):
        """
        Calculate this service's rank among all services with the same name.
        WARNING: This queries ALL services with this name on EVERY access!
        Avoid calling in loops. For bulk operations, calculate ranks in batch
        (see scoring_engine/web/views/api/team.py:42-62 for efficient implementation).
        """
        scores = (
            db.session.query(Service.team_id, func.sum(Service.points).label("score"))
            .filter(Service.scoring_up.is_(True))
            .filter(Service.name == self.name)
            .group_by(Service.team_id)
            .order_by(desc("score"))
            .all()
        )

        # Scores are already sorted descending by the query
        return _get_rank_from_scores(scores, self.team_id)

    @property
    def score_earned(self):
        """
        Current score for this service: its points while the latest check is up,
        otherwise zero.
        """
        return int(self.points or 0) if self.scoring_up else 0

    @property
    def max_score(self):
        """
        Maximum current-state score for this service.
        """
        return int(self.points or 0)

    @property
    def percent_earned(self):
        if self.max_score == 0:
            return 0
        return int((self.score_earned / self.max_score) * 100)

    @property
    def last_ten_checks(self):
        """
        Get the last 10 checks for this service in reverse chronological order.
        Optimized to use a DB query with LIMIT instead of loading all checks into memory.
        """
        return db.session.query(Check).filter(Check.service_id == self.id).order_by(desc(Check.id)).limit(10).all()

    @property
    def consecutive_failures(self):
        """
        Count consecutive failures for this service from the most recent check.
        Returns 0 if the most recent check passed.
        """
        from scoring_engine.sla import get_consecutive_failures

        return get_consecutive_failures(self.id)

    @property
    def sla_penalty_percent(self):
        """
        Get the current SLA penalty percentage for this service.
        """
        from scoring_engine.sla import calculate_sla_penalty_percent, get_sla_config

        config = get_sla_config()
        return calculate_sla_penalty_percent(self.consecutive_failures, config)

    @property
    def sla_penalty_points(self):
        """
        Get the penalty points to deduct from this service's score.
        """
        from scoring_engine.sla import calculate_service_penalty_points

        return calculate_service_penalty_points(self)

    @property
    def adjusted_score(self):
        """
        Get the score for this service after applying SLA penalties.
        """
        from scoring_engine.sla import calculate_service_adjusted_score

        return calculate_service_adjusted_score(self)

    @property
    def sla_status(self):
        """
        Get comprehensive SLA status for this service.
        """
        from scoring_engine.sla import get_service_sla_status

        return get_service_sla_status(self)
