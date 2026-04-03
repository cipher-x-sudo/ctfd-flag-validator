"""
Track which challenges have had first blood announced (poller + solve() idempotency).
"""
from CTFd.models import db


class ContainerFirstBloodAnnounced(db.Model):
    """
    One row per challenge that has had first blood announced.
    Used by the first-blood poller and by solve() to avoid double announcements.
    """
    __tablename__ = "container_first_blood_announced"

    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
