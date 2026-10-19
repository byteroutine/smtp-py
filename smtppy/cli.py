from ._version import __version__
import asyncio
import os
import secrets
from contextlib import asynccontextmanager

import typer
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator, model_validator
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import env_file_info, get_settings, require_api_token, validate_smtp_settings
from .database import fetch_recent, get_stats, init_db, reset_retryable
from .relay import dispatch_bulk, normalize_recipients, parse_compose_file, parse_recipients_file

security = HTTPBearer(auto_error=False)
console = Console()
cli = typer.Typer(no_args_is_help=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> bool:
    expected = require_api_token()
    provided = credentials.credentials if credentials else ""
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )
    return True


class SendRequest(BaseModel):
    to: str | list[str]
    subject: str
    text: str | None = None
    html: str | None = None

    @field_validator("subject")
    @classmethod
    def subject_not_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("subject must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def body_present(self) -> "SendRequest":
        if not self.text and not self.html:
            raise ValueError("provide at least one of text or html")
        return self


class SendResponse(BaseModel):
    queued: int
    recipients: list[str]


class StatsResponse(BaseModel):
    total: int
    queued: int
    sending: int
    retrying: int
    sent: int
    failed: int


def _run_dispatch(
    recipients: list[str],
    subject: str,
    text: str | None,
    html: str | None,
) -> None:
    asyncio.run(dispatch_bulk(recipients, subject, text, html))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    require_api_token(settings)
    validate_smtp_settings(settings)
    init_db()
    yield


app = FastAPI(title="smtp-py", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/stats", response_model=StatsResponse)
def api_stats(_: bool = Depends(require_token)) -> dict:
    init_db()
    raw = get_stats()
    return {
        "total": raw.get("TOTAL", 0),
        "queued": raw.get("QUEUED", 0),
        "sending": raw.get("SENDING", 0),
        "retrying": raw.get("RETRYING", 0),
        "sent": raw.get("SENT", 0),
        "failed": raw.get("FAILED", 0),
    }


@app.get("/api/recent")
def api_recent(limit: int = 50, _: bool = Depends(require_token)) -> dict:
    init_db()
    safe_limit = max(1, min(limit, 200))
    items = fetch_recent(safe_limit)
    return {"items": items}


@app.post("/api/send", response_model=SendResponse, status_code=status.HTTP_202_ACCEPTED)
def api_send(
    payload: SendRequest,
    background: BackgroundTasks,
    _: bool = Depends(require_token),
) -> dict:
    try:
        recipients = normalize_recipients(payload.to)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    background.add_task(_run_dispatch, recipients, payload.subject, payload.text, payload.html)
    return {"queued": len(recipients), "recipients": recipients}


def _resolve_body_text(
    body: str | None,
    body_file: str | None,
) -> str | None:
    if body:
        return body
    if body_file:
        with open(body_file, "r", encoding="utf-8") as handle:
            return handle.read()
    return None


def _resolve_body_html(html_file: str | None) -> str | None:
    if html_file:
        with open(html_file, "r", encoding="utf-8") as handle:
            return handle.read()
    return None


def send_batch(
    recipients: list[str],
    subject: str,
    body_text: str | None,
    body_html: str | None = None,
) -> dict:
    if not recipients:
        raise ValueError("No recipients provided")
    clean_subject = (subject or "").strip()
    if not clean_subject:
        raise ValueError("Subject must not be empty")
    if not (body_text and body_text.strip()) and not body_html:
        raise ValueError("Provide a text body or HTML content")
    validate_smtp_settings()
    init_db()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    )
    summary: dict = {}

    def on_progress(done: int, total: int, recipient: str, ok: bool) -> None:
        progress.update(task_id, completed=done, description=f"Sending to {recipient}")

    with progress:
        task_id = progress.add_task("Sending emails", total=len(recipients))

        async def runner() -> None:
            result = await dispatch_bulk(
                recipients,
                clean_subject,
                body_text,
                body_html,
                on_progress=on_progress,
            )
            summary.update(result)

        asyncio.run(runner())

    table = Table(title="Dispatch summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total", str(summary.get("total", 0)))
    table.add_row("Sent", str(summary.get("sent", 0)))
    table.add_row("Failed", str(summary.get("failed", 0)))
    console.print(table)
    return summary


@cli.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        from .menu import run_menu

        run_menu()


@cli.command("send")
def cli_send(
    file: str | None = typer.Option(None, "--file", "-f", help="Path to line-delimited emails.txt"),
    subject: str | None = typer.Option(None, "--subject", "-s", help="Email subject"),
    body: str | None = typer.Option(None, "--body", "-b", help="Plain text body"),
    body_file: str | None = typer.Option(None, "--body-file", help="Path to plain text body file"),
    html_file: str | None = typer.Option(None, "--html-file", help="Path to HTML body file"),
    compose: str | None = typer.Option(None, "--compose", "-c", help="Path to writemail.txt compose file"),
) -> None:
    try:
        if compose:
            data = parse_compose_file(compose)
            recipients = data["recipients"]
            if recipients:
                console.print(f"Loaded compose file {compose}: {len(recipients)} recipient(s)")
            else:
                raw = typer.prompt("Recipients (comma or newline separated)")
                recipients = normalize_recipients(raw)
            final_subject = subject or data["subject"]
            final_text = body or data["body_text"]
            if body_file:
                final_text = _resolve_body_text(None, body_file)
            final_html = data["body_html"]
            if html_file:
                final_html = _resolve_body_html(html_file)
            console.print(f"Loaded compose file {compose}: {len(recipients)} recipient(s)")
        elif file:
            recipients = parse_recipients_file(file)
            console.print(f"Loaded {len(recipients)} recipients from {file}")
            final_subject = subject or typer.prompt("Subject")
            final_text = _resolve_body_text(body, body_file)
            if final_text is None:
                final_text = typer.prompt("Body (plain text)", default="")
            final_html = _resolve_body_html(html_file)
        else:
            raw = typer.prompt("Recipients (comma or newline separated)")
            recipients = normalize_recipients(raw)
            final_subject = subject or typer.prompt("Subject")
            final_text = _resolve_body_text(body, body_file)
            if final_text is None:
                final_text = typer.prompt("Body (plain text)", default="")
            final_html = _resolve_body_html(html_file)
        send_batch(recipients, final_subject, final_text, final_html)
    except ValueError as exc:
        console.print(Panel(f"[red]{exc}[/red]", title="Send error", border_style="red"))
        raise typer.Exit(code=1)
    except FileNotFoundError as exc:
        console.print(Panel(f"[red]File not found: {exc.filename}[/red]", title="Send error", border_style="red"))
        raise typer.Exit(code=1)


@cli.command("serve")
def cli_serve(
    host: str | None = typer.Option(None, "--host", help="Bind host"),
    port: int | None = typer.Option(None, "--port", help="Bind port"),
) -> None:
    settings = get_settings()
    try:
        require_api_token(settings)
        validate_smtp_settings(settings)
    except ValueError as exc:
        console.print(Panel(f"[red]{exc}[/red]", title="Configuration error", border_style="red"))
        raise typer.Exit(code=1)
    uvicorn.run(
        "main:app",
        host=host or settings.app_host,
        port=port or settings.app_port,
        log_level="info",
    )


@cli.command("stats")
def cli_stats() -> None:
    init_db()
    raw = get_stats()
    table = Table(title="smtp-py stats")
    table.add_column("Status", style="cyan")
    table.add_column("Count", style="green")
    for key in ("QUEUED", "SENDING", "RETRYING", "SENT", "FAILED", "TOTAL"):
        table.add_row(key, str(raw.get(key, 0)))
    console.print(table)


@cli.command("recent")
def cli_recent(
    limit: int = typer.Option(20, "--limit", "-n", help="How many rows to show"),
) -> None:
    init_db()
    rows = fetch_recent(max(1, min(limit, 200)))
    table = Table(title="Recent transmissions")
    table.add_column("ID", style="cyan")
    table.add_column("Recipient")
    table.add_column("Subject")
    table.add_column("Status", style="magenta")
    table.add_column("Attempts", style="green")
    table.add_column("Updated")
    for row in rows:
        table.add_row(
            str(row.get("id")),
            str(row.get("recipient")),
            str(row.get("subject"))[:40],
            str(row.get("status")),
            str(row.get("attempts")),
            str(row.get("updated_at")),
        )
    console.print(table)


@cli.command("retry-failed")
def cli_retry_failed() -> None:
    init_db()
    count = reset_retryable()
    console.print(f"[green]Re-queued {count} failed transmissions[/green]")


def _masked_secret(value: str | None) -> str:
    if not value or not value.strip():
        return "[red]NOT SET[/red]"
    return f"[green]set[/green] ({len(value.strip())} chars)"


def diagnose_report() -> None:
    info = env_file_info()
    settings = get_settings()
    table = Table(title="smtp-py diagnostics")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_row("Working directory", os.getcwd())
    table.add_row(".env path checked", info["path"])
    if info["exists"]:
        table.add_row(".env file", f"[green]found[/green] ({info['size']} bytes)")
    else:
        table.add_row(".env file", "[red]NOT FOUND[/red] - copy .env.example to .env in this folder")
    table.add_row("SMTP_HOST", settings.smtp_host)
    table.add_row("SMTP_PORT", str(settings.smtp_port))
    table.add_row("SMTP_USERNAME", settings.smtp_username or "[red]NOT SET[/red]")
    table.add_row("SMTP_PASSWORD", _masked_secret(settings.smtp_password))
    table.add_row("SMTP_FROM_EMAIL", settings.smtp_from_email)
    table.add_row("SMTP_USE_SSL / TLS", f"{settings.smtp_use_ssl} / {settings.smtp_use_tls}")
    table.add_row("API_BEARER_TOKEN", _masked_secret(settings.api_bearer_token))
    table.add_row("DATABASE_PATH", os.path.abspath(settings.database_path))
    try:
        validate_smtp_settings(settings)
        table.add_row("SMTP validation", "[green]OK[/green]")
    except ValueError as exc:
        table.add_row("SMTP validation", f"[red]FAILED[/red]\n{exc}")
    try:
        require_api_token(settings)
        table.add_row("API token check", "[green]OK[/green]")
    except ValueError as exc:
        table.add_row("API token check", f"[red]FAILED[/red]\n{exc}")
    console.print(table)


@cli.command("diagnose")
def cli_diagnose() -> None:
    diagnose_report()


if __name__ == "__main__":
    cli()
