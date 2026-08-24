from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import console, metrics, pipeline
from app.db import get_db, init_db, schema_drift
from app.models import AuditLog, FailedPayment
from app.schemas import (
    AuditEntryOut,
    FailedPaymentOut,
    OutcomeEnvelope,
    ResolveRequest,
    WebhookEnvelope,
)

app = FastAPI(title="AI Revenue Recovery")


@app.on_event("startup")
def on_startup() -> None:
    init_db()

    # Fail loudly on a stale database rather than 500ing on every request. This project
    # has no migrations, so a schema change means reseeding, and the previous symptom
    # was an OperationalError per request that read like a server bug.
    drift = schema_drift()
    if drift:
        detail = "\n  ".join(drift)
        raise RuntimeError(
            f"Database schema is behind the models:\n  {detail}\n"
            f"There are no migrations in this project. Delete recovery.db and reseed:\n"
            f"  python -m scripts.seed_synthetic_data 300"
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhooks/razorpay")
def razorpay_webhook(envelope: WebhookEnvelope, db: Session = Depends(get_db)):
    payment = pipeline.ingest_event(db, envelope.event_id, envelope.event, envelope.payload)
    return {"processed": payment is not None, "failed_payment_id": payment.id if payment else None}


@app.post("/webhooks/razorpay/outcome")
def razorpay_outcome_webhook(envelope: OutcomeEnvelope, db: Session = Depends(get_db)):
    payment = pipeline.ingest_outcome(db, envelope.event_id, envelope.razorpay_payment_id, envelope.success)
    if payment is None:
        raise HTTPException(status_code=404, detail="no matching failed payment for outcome event")
    return {"status": payment.status}


@app.get("/payments", response_model=list[FailedPaymentOut])
def list_payments(status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(FailedPayment).order_by(FailedPayment.first_failed_at.desc())
    if status:
        stmt = stmt.where(FailedPayment.status == status)
    return db.scalars(stmt).all()


@app.get("/payments/{payment_id}/audit", response_model=list[AuditEntryOut])
def payment_audit_trail(payment_id: str, db: Session = Depends(get_db)):
    payment = db.get(FailedPayment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="failed payment not found")
    stmt = (
        select(AuditLog)
        .where(AuditLog.failed_payment_id == payment_id)
        .order_by(AuditLog.id.asc())
    )
    return db.scalars(stmt).all()


@app.get("/metrics/overview")
def metrics_overview(db: Session = Depends(get_db)):
    return metrics.overview(db)


@app.get("/metrics/compliance")
def metrics_compliance(db: Session = Depends(get_db)):
    return metrics.compliance_invariants(db)


@app.get("/metrics/bandit")
def metrics_bandit(db: Session = Depends(get_db)):
    return metrics.bandit_arms(db)


@app.get("/metrics/stop-reasons")
def metrics_stop_reasons(db: Session = Depends(get_db)):
    return metrics.stop_reasons(db)


@app.get("/metrics/rules-fired")
def metrics_rules_fired(db: Session = Depends(get_db)):
    return metrics.rules_fired(db)


# --- live agent console -------------------------------------------------------


@app.get("/agent/activity")
def agent_activity(
    limit: int = 60,
    before_id: int | None = None,
    after_id: int | None = None,
    db: Session = Depends(get_db),
):
    """The decision stream. Pass `after_id` with the highest id already on screen to
    tail only what is new; pass `before_id` to page back through history."""
    return console.activity(db, limit=min(limit, 200), before_id=before_id, after_id=after_id)


@app.get("/agent/queue")
def agent_queue(db: Session = Depends(get_db)):
    return console.review_queue(db)


@app.get("/agent/pulse")
def agent_pulse(db: Session = Depends(get_db)):
    return console.pulse(db)


@app.post("/agent/queue/{payment_id}/resolve")
def agent_resolve(payment_id: str, body: ResolveRequest, db: Session = Depends(get_db)):
    """The one place a human writes back. Refusals are 409 rather than 500 — a blocked
    operator action is an expected outcome of the rules working, not a server fault."""
    try:
        payment = console.resolve(
            db, payment_id, outcome=body.outcome, operator=body.operator,
            note=body.note,
        )
    except console.OperatorActionRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": payment.status, "payment_id": payment.id}


@app.get("/console", include_in_schema=False)
def agent_console():
    return FileResponse(Path(__file__).parent / "static" / "console.html")


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
