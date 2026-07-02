"""Data access for WithdrawRequest (S79)."""
from typing import List, Optional
from uuid import UUID

from plugins.withdraw.withdraw.models.withdraw_request import WithdrawRequest


class WithdrawRequestRepository:
    """Exactly the queries WithdrawService needs — nothing speculative."""

    def __init__(self, session) -> None:
        self._session = session

    def create(self, request: WithdrawRequest) -> WithdrawRequest:
        self._session.add(request)
        self._session.commit()
        return request

    def save(self, request: WithdrawRequest) -> WithdrawRequest:
        self._session.add(request)
        self._session.commit()
        return request

    def delete(self, request: WithdrawRequest) -> None:
        self._session.delete(request)
        self._session.commit()

    def find_by_id(self, request_id: UUID) -> Optional[WithdrawRequest]:
        return (
            self._session.query(WithdrawRequest)
            .filter(WithdrawRequest.id == request_id)
            .first()
        )

    def find_by_user_id(self, user_id: UUID) -> List[WithdrawRequest]:
        """A user's own requests, newest first."""
        return (
            self._session.query(WithdrawRequest)
            .filter(WithdrawRequest.user_id == user_id)
            .order_by(WithdrawRequest.created_at.desc())
            .all()
        )

    def find_all(self, status: Optional[str] = None) -> List[WithdrawRequest]:
        """All requests (admin view), optionally filtered, newest first."""
        query = self._session.query(WithdrawRequest)
        if status:
            query = query.filter(WithdrawRequest.status == status)
        return query.order_by(WithdrawRequest.created_at.desc()).all()
