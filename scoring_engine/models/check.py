import html
from datetime import datetime, timezone

import pytz
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, Text, UnicodeText
from sqlalchemy.orm import relationship

from scoring_engine.config import config
from scoring_engine.datetime_utils import ensure_utc_aware
from scoring_engine.models.base import Base


class Check(Base):
    __tablename__ = "checks"
    # ``checks`` is by far the largest table (services x rounds).  InnoDB gives
    # us implicit single-column indexes on the two FK columns and nothing else,
    # which leaves every composite/covering access path doing a full scan.
    __table_args__ = (
        # sla.get_consecutive_failures / get_max_consecutive_failures and
        # Service.checks_reversed: WHERE service_id = ? ORDER BY round_id.
        # Without this, MySQL scans every check row and filesorts.
        Index("ix_checks_service_id_round_id", "service_id", "round_id"),
        # Service history views: WHERE service_id = ? AND result = 1.
        Index("ix_checks_service_id_result", "service_id", "result"),
        # api/overview.py num_up_services / num_down_services and
        # api/admin.py round stats: WHERE round_id = ? AND result = ?.
        Index("ix_checks_round_id_result", "round_id", "result"),
        # scores.team_penalties (batched SLA): the last-passing-round MAX and the
        # trailing-failure COUNT both group by service_id and filter on
        # completed/result with a round_id bound. Ordering the columns as
        # (service_id, completed, result, round_id) lets both run as index-only
        # loose scans instead of scanning the whole (huge) checks table.
        Index("ix_checks_sla_scan", "service_id", "completed", "result", "round_id"),
    )
    id = Column(Integer, primary_key=True)
    round_id = Column(Integer, ForeignKey("rounds.id"))
    round = relationship("Round", back_populates="checks")
    service_id = Column(Integer, ForeignKey("services.id"))
    service = relationship("Service")
    result = Column(Boolean)
    output = Column(UnicodeText, default="")
    reason = Column(Text, default="")
    command = Column(Text, default="")
    completed_timestamp = Column(DateTime)
    completed = Column(Boolean, default=False)

    def finished(self, result, reason, output, command):
        self.result = result
        self.reason = reason
        self.output = html.escape(output)
        self.completed = True
        self.completed_timestamp = datetime.now(timezone.utc)
        self.command = command

    @property
    def local_completed_timestamp(self):
        return (
            ensure_utc_aware(self.completed_timestamp)
            .astimezone(pytz.timezone(config.timezone))
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
