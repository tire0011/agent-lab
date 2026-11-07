import re

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from models import (
    MailboxMessageEntry,
    MailboxMessagesResponse,
    MailboxTaskApproval,
    MailboxTaskApprovalsResponse,
)
from modules.experiment_service import ExperimentService
from modules.mailbox.task_service import MailboxTaskService

from api import LIMIT_MIN, app, get_experiment_service, get_mailbox_task_service

router = APIRouter(dependency_overrides_provider=app)


MAILBOX_TASKS_DEFAULT_LIMIT = 20
MAILBOX_TASKS_MAX_LIMIT = 100




def _approval_to_response(approval) -> MailboxTaskApproval:
    return MailboxTaskApproval(**approval.to_dict())


@router.get("/mailbox/tasks", response_model=MailboxTaskApprovalsResponse)
def list_mailbox_tasks(
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=MAILBOX_TASKS_DEFAULT_LIMIT, ge=LIMIT_MIN, le=MAILBOX_TASKS_MAX_LIMIT),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApprovalsResponse:
    authorize(x_api_key)
    tasks = service.list_recent(limit=limit)
    return MailboxTaskApprovalsResponse(
        count=len(tasks),
        tasks=[_approval_to_response(t) for t in tasks],
    )


@router.get("/mailbox/tasks/{approval_id}", response_model=MailboxTaskApproval)
def get_mailbox_task(
    approval_id: str,
    x_api_key: str | None = Header(default=None),
    service: MailboxTaskService = Depends(get_mailbox_task_service),
) -> MailboxTaskApproval:
    authorize(x_api_key)
    try:
        approval = service.get(approval_id)
    except ApprovalNotFoundError:
        raise HTTPException(status_code=404, detail="approval not found")
    return _approval_to_response(approval)


@router.post("/mailbox/webhook/github")
async def mailbox_github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV)
    if not secret:
        _logger.error("mailbox_webhook failed reason=missing_secret")
        raise HTTPException(status_code=503, detail="webhook not configured")

    raw_body = await request.body()
    if not verify_signature(
        secret=secret,
        raw_body=raw_body,
        signature_header=x_hub_signature_256,
    ):
        _logger.warning("mailbox_webhook rejected reason=invalid_signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _logger.warning("mailbox_webhook rejected reason=invalid_json")
        raise HTTPException(status_code=400, detail="invalid json")

    if is_ping_event(x_github_event):
        _logger.info("mailbox_webhook ignored reason=ping")
        return JSONResponse(status_code=200, content={"status": "pong"})

    if (x_github_event or "").strip().lower() != "push":
        _logger.info("mailbox_webhook ignored reason=event")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    if not isinstance(payload, dict) or not has_inbox_changes(payload):
        _logger.info("mailbox_webhook ignored reason=no_inbox_changes")
        return JSONResponse(status_code=200, content={"status": "ignored"})

    if not repository_matches(payload, expected_repository()):
        _logger.warning("mailbox_webhook ignored reason=unexpected_repository")
        return JSONResponse(status_code=202, content={"status": "ignored"})

    _start_mailbox_webhook_poll()
    _logger.info("mailbox_webhook accepted reason=inbox_change")
    return JSONResponse(status_code=202, content={"status": "accepted"})


# Public, sanitized inbox/outbox listing: shows what came in and went out without
# ever exposing full prompts, file paths, or secrets.

MAILBOX_MESSAGES_DEFAULT_LIMIT = 20
MAILBOX_MESSAGES_MAX_LIMIT = 100

_MAILBOX_TYPE_LABELS_DE = {
    "idea": "Idee",
    "task": "Auftrag",
    "question": "Frage",
    "context": "Kontext",
    "task_approval_result": "Auftragsergebnis",
    "failure_analysis": "Fehleranalyse",
}

_SAFE_SENDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _mailbox_type_label(msg_type: str | None) -> str:
    if not msg_type:
        return "Nachricht"
    return _MAILBOX_TYPE_LABELS_DE.get(msg_type, msg_type.replace("_", " ").strip().capitalize() or "Nachricht")


def _safe_mailbox_sender(raw: str | None) -> str | None:
    """Only expose sender/creator identifiers matching a safe, narrow shape."""
    if not raw:
        return None
    candidate = raw.strip()
    return candidate if _SAFE_SENDER_RE.fullmatch(candidate) else None


def _mailbox_preview(text: str | None) -> str | None:
    if not text:
        return None
    # Drop markdown headings so the preview starts with actual content, then redact
    # and bound it like every other diagnostic.
    stripped = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    ).strip()
    return redaction.sanitized_reason(stripped, max_chars=220)


def _inbox_entries(service: ExperimentService, limit: int) -> list[MailboxMessageEntry]:
    rows = mailbox_module.list_recent_db_messages(service._conn, limit=limit)
    entries = []
    for row in rows:
        preview = _mailbox_preview(mailbox_module.read_inbox_preview(row.get("file_path")))
        entries.append(
            MailboxMessageEntry(
                id=f"msg-{row['id']}",
                timestamp=row.get("received_at") or "",
                type=row.get("msg_type") or "unknown",
                status=row.get("status") or "unknown",
                sender=_safe_mailbox_sender(row.get("created_by")),
                title=_mailbox_type_label(row.get("msg_type")),
                preview=preview,
            )
        )
    return entries


def _outbox_entries(limit: int) -> list[MailboxMessageEntry]:
    files = mailbox_module.list_outbox_files(_OUTBOX_DIR, limit=limit)
    entries = []
    for item in files:
        meta = item.get("meta") or {}
        msg_type = meta.get("type")
        entries.append(
            MailboxMessageEntry(
                id=item["filename"],
                timestamp=meta.get("processed_at") or meta.get("created_at") or "",
                type=msg_type or "unknown",
                status=meta.get("status") or "unknown",
                sender=_safe_mailbox_sender(meta.get("created_by") or meta.get("sender")),
                title=_mailbox_type_label(msg_type),
                preview=_mailbox_preview(item.get("body")),
            )
        )
    return entries


@router.get("/mailbox/messages", response_model=MailboxMessagesResponse)
def list_mailbox_messages(
    limit: int = Query(default=MAILBOX_MESSAGES_DEFAULT_LIMIT, ge=LIMIT_MIN, le=MAILBOX_MESSAGES_MAX_LIMIT),
    service: ExperimentService = Depends(get_experiment_service),
) -> MailboxMessagesResponse:
    inbox = _inbox_entries(service, limit)
    outbox = _outbox_entries(limit)
    return MailboxMessagesResponse(
        inbox_count=len(inbox),
        outbox_count=len(outbox),
        inbox=inbox,
        outbox=outbox,
    )
