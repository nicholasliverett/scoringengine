"""Sparse per-round snapshots of each team's current service-state score."""

from sqlalchemy import Column, ForeignKey, Index, Integer, UniqueConstraint

from scoring_engine.models.base import Base


class ServiceScoreSnapshot(Base):
    __tablename__ = "service_score_snapshot"
    __table_args__ = (
        UniqueConstraint("round_id", "team_id", name="_service_score_snapshot_round_team_uc"),
        Index("ix_service_score_snapshot_team_round", "team_id", "round_number"),
        Index("ix_service_score_snapshot_round_number", "round_number"),
    )

    id = Column(Integer, primary_key=True)
    round_id = Column(Integer, ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False)
    round_number = Column(Integer, nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    service_points = Column(Integer, nullable=False, default=0)
