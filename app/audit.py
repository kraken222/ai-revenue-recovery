from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(
    db: Session,
    *,
    failed_payment_id: str | None,
    stage: str,
    actor: str,
    detail: dict,
    now: datetime | None = None,
) -> None:
    kwargs = {"failed_payment_id": failed_payment_id, "stage": stage, "actor": actor, "detail": detail}
    if now is not None:
        kwargs["created_at"] = now
    db.add(AuditLog(**kwargs))
