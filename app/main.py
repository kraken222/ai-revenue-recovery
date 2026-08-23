from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import metrics, pipeline
from app.db import get_db, init_db
from app.models import AuditLog, FailedPayment
from app.schemas import AuditEntryOut, FailedPaymentOut, OutcomeEnvelope, WebhookEnvelope

app = FastAPI(title="AI Revenue Recovery")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


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


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
