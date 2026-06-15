"""WithdrawRequest model — one row per withdraw-to-money request (S79).

Status flow (D6): pending → approved → processing → completed, with the
terminal failures failed / rejected. The balance is debited at request
time (double-spend prevention); failed/rejected refunds it.
"""
from sqlalchemy.dialects.postgresql import JSONB, UUID

from vbwd.extensions import db
from vbwd.models.base import BaseModel


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"

VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
)

DEFAULT_BALANCE_SOURCE = "tokens"


class WithdrawRequest(BaseModel):
    """A user's request to convert balance-source units into money."""

    __tablename__ = "withdraw_request"

    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    balance_source = db.Column(
        db.String(50), nullable=False, default=DEFAULT_BALANCE_SOURCE
    )
    # amount in source units (e.g. tokens)
    amount = db.Column(db.Integer, nullable=False)
    # converted money value sent to the payout provider
    payout_amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    provider = db.Column(db.String(50), nullable=False)
    destination = db.Column(JSONB, nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default=STATUS_PENDING, index=True
    )
    provider_payout_id = db.Column(db.String(255), nullable=True)
    error = db.Column(db.String(512), nullable=True)

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "balance_source": self.balance_source,
            "amount": self.amount,
            "payout_amount": str(self.payout_amount),
            "currency": self.currency,
            "provider": self.provider,
            "destination": self.destination,
            "status": self.status,
            "provider_payout_id": self.provider_payout_id,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<WithdrawRequest(user_id={self.user_id}, amount={self.amount}, "
            f"provider={self.provider}, status={self.status})>"
        )
