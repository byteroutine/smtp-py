import os

import uvicorn
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.theme import Theme

from .banner import animate_header, header_ansi, play_banner
from .config import get_settings, require_api_token, validate_smtp_settings
from .database import fetch_by_status, fetch_recent, get_stats, init_db, reset_retryable
from ._version import __version__
from .cli import diagnose_report, send_batch
from .relay import (
    normalize_recipients,
    parse_compose_file,
    parse_recipients_file,
    test_smtp_connection,
)

THEME = Theme(
    {
        "header": "bold cyan",
        "accent": "bold green",
        "soft": "dim white",
        "danger": "bold red",
        "warn": "yellow",
    }
)

console = Console(theme=THEME)

MENU_ITEMS = [
    ("1", "Send Email", "Interactive, emails.txt, or writemail.txt compose file"),
    ("2", "Start API Server", "Launch the FastAPI + Uvicorn gateway"),
    ("3", "Email Statistics", "Totals by status from the SQLite log"),
    ("4", "Recent Email Activity", "Latest transmissions in a table"),
    ("5", "Retry Failed Emails", "Requeue FAILED transmissions (with confirmation)"),
    ("6", "Test SMTP Connection", "Verify login without sending any email"),
    ("7", "Diagnose Setup", "Show exactly what config the app loaded"),
    ("8", "About", "What smtp-py is, plus repo and developer links"),
    ("0", "Exit", "Close the menu cleanly"),
]


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def show_header() -> None:
    from rich.text import Text

    animate_header(console, console.width)
    console.print(
        Panel(
            Align.center(Text.from_ansi(header_ansi(console.width))),
            title=f"[header]smtp-py v{__version__}[/header]",
            subtitle="[soft]One line to send. Zero services to pay for.[/soft]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def show_menu() -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Option", style="accent", justify="right", width=8)
    table.add_column("Action", style="header")
    table.add_column("Description", style="soft")
    for number, action, description in MENU_ITEMS:
        table.add_row(f"[ {number} ]", action, description)
    console.print()
    console.print(table)
    console.print()


def pause() -> None:
    try:
        Prompt.ask("[soft]Press Enter to return to the menu[/soft]", default="")
    except (KeyboardInterrupt, EOFError):
        console.print()


def show_error(message: str) -> None:
    console.print(Panel(f"[danger]{message}[/danger]", title="Error", border_style="red"))


def show_ok(message: str, title: str = "Done") -> None:
    console.print(Panel(f"[accent]{message}[/accent]", title=title, border_style="green"))


def _style_status(value: str) -> str:
    styles = {
        "SENT": "green",
        "FAILED": "red",
        "RETRYING": "yellow",
        "SENDING": "cyan",
        "QUEUED": "dim",
    }
    return styles.get(value, "white")


def _error_guidance(error_text: str) -> str:
    lowered = error_text.lower()
    if "535" in lowered or "authentication failed" in lowered:
        return (
            "The SMTP server rejected the login. Double-check SMTP_PASSWORD in your .env "
            "file (SpaceMail login is the full email address). In Spacemail webmail, open "
            "Settings and make sure IMAP/SMTP connections are enabled for this mailbox. "
            "If the account uses 2-step verification, use an app-specific password."
        )
    if "550" in lowered or "553" in lowered or "mailbox unavailable" in lowered:
        return "The recipient address was rejected by the server. Fix or remove it and try again."
    if "timed out" in lowered or "timeout" in lowered or "connection failed" in lowered:
        return (
            "Could not reach the SMTP server. Check that your firewall allows outbound "
            "port 465 and that SMTP_HOST is mail.spacemail.com."
        )
    if "421" in lowered or "throttl" in lowered:
        return "The server is throttling you. Lower RATE_LIMIT_PER_SECOND and retry later."
    return "See option 4 (Recent Email Activity) for the full stored error text."


def _show_failed_errors(database_path: str | None) -> None:
    rows = fetch_by_status("FAILED", limit=3, database_path=database_path)
    if not rows:
        return
    lines = []
    for row in rows:
        lines.append(f"[red]{row.get('recipient')}:[/red] {row.get('last_error')}")
    lines.append("")
    lines.append(f"[yellow]What to do:[/yellow] {_error_guidance(str(rows[0].get('last_error')))}")
    console.print(Panel("\n".join(lines), title="Failed deliveries", border_style="red"))


def _preview_and_confirm(recipients: list[str], subject: str, body_text: str | None) -> bool:
    preview = (body_text or "").strip().replace("\n", " ")
    if len(preview) > 160:
        preview = preview[:157] + "..."
    console.print(
        Panel(
            f"[header]To:[/header] {len(recipients)} recipient(s)\n"
            f"[header]Subject:[/header] {subject}\n"
            f"[header]Body:[/header] {preview}",
            title="Ready to send",
            border_style="yellow",
        )
    )
    try:
        return Confirm.ask("Send now?", default=False)
    except (KeyboardInterrupt, EOFError):
        return False


def _choose_recipients() -> list[str] | None:
    console.print(Panel("[header]Step 1 - Recipients[/header]", border_style="cyan"))
    console.print("[accent][ 1 ][/accent] Type recipients manually")
    console.print("[accent][ 2 ][/accent] Load from emails.txt (in this folder)")
    console.print("[accent][ 0 ][/accent] Back to main menu")
    try:
        mode = Prompt.ask("Choose", default="1").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if mode == "0":
        return None
    if mode == "2":
        recipients = parse_recipients_file("emails.txt")
        console.print(f"[soft]Loaded {len(recipients)} recipient(s) from emails.txt[/soft]")
        return recipients
    if mode != "1":
        show_error("Invalid choice. Pick 1, 2, or 0.")
        return None
    raw = Prompt.ask("Recipients (comma separated)").strip()
    return normalize_recipients(raw)


def _choose_content() -> tuple[str, str | None, str | None] | None:
    console.print(Panel("[header]Step 2 - Content[/header]", border_style="cyan"))
    console.print("[accent][ 1 ][/accent] Type subject and body manually")
    console.print("[accent][ 3 ][/accent] Load from writemail.txt (in this folder)")
    console.print("[accent][ 0 ][/accent] Back to main menu")
    try:
        mode = Prompt.ask("Choose", default="1").strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if mode == "0":
        return None
    if mode == "3":
        data = parse_compose_file("writemail.txt")
        console.print("[soft]Loaded subject and body from writemail.txt[/soft]")
        return data["subject"], data["body_text"], data["body_html"]
    if mode != "1":
        show_error("Invalid choice. Pick 1, 3, or 0.")
        return None
    subject = Prompt.ask("Subject").strip()
    body_text = Prompt.ask("Body (plain text)", default="")
    html_path = Prompt.ask("HTML file (optional, Enter to skip)", default="").strip()
    body_html = None
    if html_path:
        with open(html_path, "r", encoding="utf-8") as handle:
            body_html = handle.read()
    return subject, body_text, body_html


def action_send() -> None:
    console.print(Panel("[header]Send Email[/header]", border_style="cyan"))
    try:
        recipients = _choose_recipients()
        if not recipients:
            console.print("[soft]Cancelled. Nothing was sent.[/soft]")
            return
        content = _choose_content()
        if content is None:
            console.print("[soft]Cancelled. Nothing was sent.[/soft]")
            return
        subject, body_text, body_html = content
        if not _preview_and_confirm(recipients, subject, body_text):
            console.print("[soft]Cancelled. Nothing was sent.[/soft]")
            return
        summary = send_batch(recipients, subject, body_text, body_html)
        show_ok(
            f"Sent {summary.get('sent', 0)} of {summary.get('total', 0)} "
            f"({summary.get('failed', 0)} failed). See option 4 for details.",
            title="Dispatch finished",
        )
        if summary.get("failed", 0):
            _show_failed_errors(summary.get("database_path"))
    except FileNotFoundError as exc:
        name = os.path.basename(exc.filename or "")
        if name == "emails.txt":
            show_error(
                "emails.txt not found in this folder. Create it with one email "
                "address per line (see emails.txt.example)."
            )
        elif name == "writemail.txt":
            show_error(
                "writemail.txt not found in this folder. Create it with "
                "SUBJECT:, BODY: and HTML: sections."
            )
        else:
            show_error(f"File not found: {exc.filename}")
    except ValueError as exc:
        show_error(str(exc))
    except (KeyboardInterrupt, EOFError):
        console.print("\n[warn]Send cancelled.[/warn]")


def action_serve() -> None:
    settings = get_settings()
    try:
        require_api_token(settings)
        validate_smtp_settings(settings)
    except ValueError as exc:
        show_error(str(exc))
        return
    address = f"http://{settings.app_host}:{settings.app_port}"
    console.print(
        Panel(
            f"API address: [accent]{address}[/accent]\n"
            f"Interactive docs: [accent]{address}/docs[/accent]\n"
            f"Health check: [accent]{address}/health[/accent] (no auth required)\n\n"
            "[warn]Press Ctrl+C to stop the server and return to the menu.[/warn]",
            title="Starting API server",
            border_style="green",
        )
    )
    try:
        uvicorn.run(
            "main:app",
            host=settings.app_host,
            port=settings.app_port,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass
    console.print("[accent]Server stopped.[/accent]")


def action_stats() -> None:
    init_db()
    raw = get_stats()
    table = Table(title="smtp-py statistics")
    table.add_column("Status", style="header")
    table.add_column("Count", style="accent", justify="right")
    for key in ("QUEUED", "SENDING", "RETRYING", "SENT", "FAILED"):
        table.add_row(key, str(raw.get(key, 0)))
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{raw.get('TOTAL', 0)}[/bold]")
    console.print(table)
    if raw.get("TOTAL", 0) == 0:
        console.print("[soft]No transmissions yet. Use option 1 to send your first email.[/soft]")


def action_recent() -> None:
    init_db()
    try:
        raw_limit = Prompt.ask("How many rows to show", default="20").strip()
    except (KeyboardInterrupt, EOFError):
        return
    try:
        limit = max(1, min(int(raw_limit), 200))
    except ValueError:
        console.print("[warn]Invalid number, showing 20 rows.[/warn]")
        limit = 20
    rows = fetch_recent(limit)
    if not rows:
        console.print("[soft]No transmissions found yet.[/soft]")
        return
    table = Table(title=f"Recent activity (last {len(rows)})")
    table.add_column("Recipient", style="header")
    table.add_column("Subject", style="soft")
    table.add_column("Status")
    table.add_column("Attempts", justify="right")
    table.add_column("Updated", style="soft")
    for row in rows:
        status_value = str(row.get("status"))
        table.add_row(
            str(row.get("recipient")),
            str(row.get("subject"))[:42],
            f"[{_style_status(status_value)}]{status_value}[/{_style_status(status_value)}]",
            str(row.get("attempts")),
            str(row.get("updated_at")),
        )
    console.print(table)


def action_retry() -> None:
    init_db()
    failed_count = get_stats().get("FAILED", 0)
    if failed_count == 0:
        show_ok("No failed transmissions to retry.")
        return
    console.print(f"[warn]{failed_count} failed transmission(s) found.[/warn]")
    try:
        confirmed = Confirm.ask("Requeue them for another attempt?", default=False)
    except (KeyboardInterrupt, EOFError):
        return
    if not confirmed:
        console.print("[soft]Cancelled. Nothing was changed.[/soft]")
        return
    moved = reset_retryable()
    show_ok(
        f"Requeued {moved} failed transmission(s) as QUEUED.\n"
        "They will be picked up by the next send run. Each row is claimed atomically, "
        "so concurrent workers cannot dispatch the same message twice.",
        title="Retry queued",
    )


def action_test_smtp() -> None:
    console.print(Panel("[header]Test SMTP Connection[/header]", border_style="cyan"))
    console.print("[soft]This connects and logs in only. No email is sent.[/soft]")
    try:
        result = test_smtp_connection()
    except ValueError as exc:
        show_error(str(exc))
        return
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Check", style="header")
    table.add_column("Result", style="accent")
    table.add_row(
        "TCP connection",
        "[green]OK[/green]" if result["connected"] else "[red]FAILED[/red]",
    )
    table.add_row(
        "Login",
        "[green]OK[/green]" if result["authenticated"] else "[red]FAILED[/red]",
    )
    console.print(table)
    if result["error"]:
        console.print(Panel(f"[red]{result['error']}[/red]", title="Server response", border_style="red"))
    if result["guidance"]:
        console.print(Panel(f"[yellow]{result['guidance']}[/yellow]", title="What to do", border_style="yellow"))
    if result["authenticated"]:
        show_ok("SMTP login works. You can send emails.", title="Connection test passed")


def action_diagnose() -> None:
    diagnose_report()


def action_about() -> None:
    console.print(
        Panel(
            f"[header]smtp-py[/header] v{__version__}\n\n"
            "Send email from Python in one line. A tiny batteries-included SMTP\n"
            "client with rate limiting, automatic retries, and a local send log.\n\n"
            "[accent]Developed by ByteRoutine[/accent]\n"
            "github.com/byteroutine\n\n"
            "[accent]Get the source:[/accent]\n"
            "github.com/byteroutine/smtp-py\n\n"
            "[soft]Released under the MIT License.[/soft]",
            title="About",
            border_style="cyan",
        )
    )


def run_menu() -> None:
    play_banner()
    while True:
        clear_screen()
        show_header()
        show_menu()
        try:
            choice = Prompt.ask("[header]Select an option[/header]", default="0").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[warn]Exiting smtp-py. Goodbye.[/warn]")
            return
        if choice not in ("0", "1", "2", "3", "4", "5", "6", "7", "8"):
            show_error("Invalid selection. Enter a number from 0 to 8.")
            pause()
            continue
        if choice == "0":
            console.print(Panel("[accent]Goodbye.[/accent]", border_style="cyan"))
            return
        try:
            if choice == "1":
                action_send()
            elif choice == "2":
                action_serve()
            elif choice == "3":
                action_stats()
            elif choice == "4":
                action_recent()
            elif choice == "5":
                action_retry()
            elif choice == "6":
                action_test_smtp()
            elif choice == "7":
                action_diagnose()
            elif choice == "8":
                action_about()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[warn]Action interrupted.[/warn]")
        except Exception as exc:
            show_error(f"Unexpected error: {exc}")
        pause()
