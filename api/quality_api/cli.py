from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import signal
import time
from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from .common import new_id, utcnow
from .config import Settings
from .db import Database
from .models import Audit, Job, Report, Run, ToolCall, UITicket


def migrate(database: Database):
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with database.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(781140701)"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def recover(database: Database, settings: Settings):
    if not settings.recovery_mode:
        raise SystemExit("Set QUALITY_RECOVERY_MODE=true and rotate QUALITY_RECOVERY_EPOCH before restore reconciliation.")
    counts = {"runs": 0, "calls": 0, "jobs": 0, "tickets": 0}
    with database.Session.begin() as db:
        if db.scalar(select(ToolCall.id).where(ToolCall.epoch == settings.recovery_epoch).limit(1)):
            raise SystemExit("The restored ledger contains the configured epoch. Generate a NEW recovery epoch first.")
        for run in db.scalars(select(Run).where(Run.state == "running").with_for_update()):
            run.state = "unknown"
            run.trace_complete = False
            counts["runs"] += 1
        for call in db.scalars(select(ToolCall).where(ToolCall.state == "claimed").with_for_update()):
            call.state = "unknown"
            counts["calls"] += 1
        for job in db.scalars(select(Job).where(Job.state.in_(["queued", "running", "processing"])).with_for_update()):
            job.state = "interrupted"
            job.error_code = "RESTORE_RECONCILIATION_REQUIRED"
            counts["jobs"] += 1
            if job.kind == "report":
                report = db.get(Report, job.entity_id)
                if report:
                    report.state = "failed"
                    report.error_code = "RESTORE_RECONCILIATION_REQUIRED"
        for ticket in db.scalars(select(UITicket).where(UITicket.consumed_at.is_(None))):
            ticket.consumed_at = utcnow()
            counts["tickets"] += 1
        db.add(Audit(id=new_id("audit"), actor_id="operator", actor_kind="worker", action="restore.quarantined",
                     entity_id="deployment", details={"epoch": settings.recovery_epoch, "counts": counts}))
    return counts


def worker(database: Database, settings: Settings, once: bool = False):
    from .routes.reports import process_next_report
    if settings.recovery_mode or settings.draining:
        raise SystemExit("Worker is disabled in recovery/drain mode")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        with database.Session.begin() as db:
            processed = process_next_report(db, settings)
        if once:
            return processed
        if not processed:
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Quality workflow maintenance; never executes source tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    runner = sub.add_parser("worker")
    runner.add_argument("--once", action="store_true")
    sub.add_parser("recover")
    demo = sub.add_parser("create-demo-ticket")
    demo.add_argument("--user", default="demo-user")
    demo.add_argument("--chat", default="demo-chat")
    demo.add_argument("--url", default="http://127.0.0.1:18080")
    args = parser.parse_args()
    settings = Settings()
    database = Database(settings.database_url)
    if args.command == "migrate":
        migrate(database)
        print("Quality database migration completed.")
    elif args.command == "worker":
        worker(database, settings, args.once)
    elif args.command == "recover":
        print(json.dumps(recover(database, settings)))
    elif args.command == "create-demo-ticket":
        if settings.environment != "development":
            raise SystemExit("Demo tickets are permitted only in explicit development mode")
        ticket = secrets.token_urlsafe(32)
        with database.Session.begin() as db:
            db.add(UITicket(id=new_id("ticket"), token_hash=hashlib.sha256(ticket.encode()).hexdigest(),
                           claims={"user_id": args.user, "instance_id": "demo-instance", "chat_id": args.chat,
                                   "case_id": None, "epoch": settings.recovery_epoch},
                           expires_at=utcnow() + timedelta(seconds=settings.ui_ticket_ttl_seconds)))
        print(args.url.rstrip("/") + "/ui/index.html#ticket=" + ticket)


if __name__ == "__main__":
    main()
