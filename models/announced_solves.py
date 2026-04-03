"""
Track announced first bloods and optionally all solves (one row per challenge_id, account_id).
Matches CTFd-First-Blood-Discord announced_solves table.
"""
from CTFd.models import db


class ContainerAnnouncedSolve(db.Model):
    """
    One row per (challenge_id, account_id) that has been announced.
    First blood = no row for challenge_id yet; "announce all solves" = one row per solver.
    account_id = team_id in team mode, user_id in user mode.
    """
    __tablename__ = "container_announced_solves"
    __table_args__ = (db.PrimaryKeyConstraint("challenge_id", "account_id"),)

    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id = db.Column(
        db.Integer,
        nullable=False,
    )
