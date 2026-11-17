"""Hashed per-team exam login credentials and once-only participation credits."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from scoring_engine.models.base import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ExamCredential(Base):
    __tablename__ = "exam_credential"
    __table_args__ = (UniqueConstraint("team_id", name="uq_exam_credential_team"),)

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    team = relationship("Team")
    user = relationship("User")


class ExamParticipation(Base):
    """Unique participation event; unique team constraint arbitrates concurrent submissions."""

    __tablename__ = "exam_participation"
    __table_args__ = (UniqueConstraint("team_id", name="uq_exam_participation_team"),)

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    score_adjustment_id = Column(Integer, ForeignKey("score_adjustment.id"), nullable=False, unique=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    team = relationship("Team")
    score_adjustment = relationship("ScoreAdjustment")
