"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import config, db, dues, gmail, ingest, issuers
from .dues import format_inr
from .models import SOURCE_MANUAL, Card, StatementRecord

console = Console()

STATUS_STYLE = {
    dues.STATUS_OVERDUE: "bold red",
    dues.STATUS_DUE_TODAY: "bold red",
    dues.STATUS_DUE_SOON: "yellow",
    dues.STATUS_UPCOMING: "green",
    dues.STATUS_SETTLED: "dim",
    dues.STATUS_NO_DATA: "dim",
    dues.STATUS_UNKNOWN_DUE_DATE: "yellow",
}

STATUS_TEXT = {
    dues.STATUS_OVERDUE: "OVERDUE",
    dues.STATUS_DUE_TODAY: "DUE TODAY",
    dues.STATUS_DUE_SOON: "Due soon",
    dues.STATUS_UPCOMING: "Upcoming",
    dues.STATUS_SETTLED: "Settled",
    dues.STATUS_NO_DATA: "No data",
    dues.STATUS_UNKNOWN_DUE_DATE: "No due date",
}


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Could not read date {value!r}; use dd/mm/yyyy")


def _select_card(conn, selector: str) -> Card:
    cards = db.list_cards(conn)
    if selector.isdigit():
        by_id = [c for c in cards if c.id == int(selector)]
        if by_id:
            return by_id[0]
        by_last4 = [c for c in cards if c.last4 == selector]
        if len(by_last4) == 1:
            return by_last4[0]
        if len(by_last4) > 1:
            raise SystemExit(f"More than one card ends in {selector}; use the card id")
    matches = [c for c in cards if selector.lower() in c.label.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No card matches {selector!r}. Run `carddues cards list`.")
    raise SystemExit(f"{selector!r} matches several cards; use the card id")


def cmd_init(args) -> int:
    conn = db.connect()
    db.init(conn)
    console.print(f"Ready. Data lives in [bold]{config.home()}[/bold]")
    return 0


def cmd_audit(args) -> int:
    """Report weak parses across every registered card."""
    from . import audit

    conn = db.connect()
    db.init(conn)
    findings = audit.audit_cards(conn)
    if not findings:
        console.print("[green]All cards look complete.[/green]")
        return 0
    table = Table(title="Parse quality")
    table.add_column("Card")
    table.add_column("Issue")
    table.add_column("Detail")
    for finding in findings:
        table.add_row(finding.card_label, finding.kind, finding.detail)
    console.print(table)
    console.print(f"[yellow]{len(findings)} finding(s)[/yellow]")
    return 1


def cmd_dump_golden(args) -> int:
    """Write text + expected stubs from the latest statement per registered card."""
    from . import passwords, pdfdoc
    from .parsers import parse_statement
    from .text import normalize

    conn = db.connect()
    db.init(conn)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cards = db.list_cards(conn)
    if not cards:
        console.print("No cards registered.")
        return 1

    written = 0
    for card in cards:
        records = db.statements_for_card(conn, card.id)
        record = dues.latest_record(records)
        if record is None or not record.source_ref:
            continue
        rows = conn.execute(
            "SELECT filename FROM ingest_log WHERE message_id = ? AND status = 'parsed' "
            "ORDER BY id DESC LIMIT 1",
            (record.source_ref,),
        ).fetchall()
        if not rows:
            continue
        filename = rows[0]["filename"]
        path = gmail.attachment_path(record.source_ref, filename)
        if not path.exists():
            console.print(f"[dim]Skip {card.label}: {path.name} not on disk[/dim]")
            continue
        candidates = passwords.candidates_for_all(cards)
        try:
            extracted = pdfdoc.extract(path, candidates)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]{card.label}: {exc}[/yellow]")
            continue
        text = normalize(extracted.text)
        parsed = parse_statement(text, issuer_hint=card.issuer, tables=extracted.tables)
        slug = f"{card.issuer}_{card.last4}"
        text_path = out / f"{slug}.txt"
        expect_path = out / f"{slug}.expected.json"
        text_path.write_text(text, encoding="utf-8")
        expected = {
            "issuer": card.issuer,
            "last4": card.last4,
            "total_due": parsed.total_due if parsed else None,
            "min_due": parsed.min_due if parsed else None,
            "due_date": parsed.due_date.isoformat() if parsed and parsed.due_date else None,
            "statement_date": (
                parsed.statement_date.isoformat()
                if parsed and parsed.statement_date
                else None
            ),
            "min_transactions": len(parsed.transactions) if parsed else 0,
        }
        expect_path.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        console.print(f"Wrote {text_path.name} + {expect_path.name}")
        written += 1
    console.print(f"Wrote {written} golden pair(s) under {out}")
    return 0 if written else 1


def cmd_auth(args) -> int:
    if gmail.client_type() == "web":
        console.print(
            "That OAuth client is a web client, which needs the browser flow. "
            "Run [bold]carddues serve[/bold] and click Connect Gmail."
        )
        return 1
    try:
        gmail.authorize(interactive=True)
    except gmail.GmailNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    console.print("[green]Gmail connected (read-only).[/green]")
    return 0


def cmd_cards_add(args) -> int:
    conn = db.connect()
    db.init(conn)
    card = Card(
        issuer=args.issuer,
        label=args.label or f"{issuers.get(args.issuer).name} ••{args.last4}",
        last4=args.last4,
        credit_limit=args.limit,
        dob=_parse_date(args.dob),
        name=args.name,
        pan=args.pan,
        extra_passwords=list(args.password or []),
    )
    card_id = db.add_card(conn, card)
    console.print(f"[green]Saved card {card.label} (id {card_id}).[/green]")
    hint = issuers.get(args.issuer).password_hint
    if hint and not args.password:
        console.print(f"[dim]Statement password hint: {hint}[/dim]")

    if db.count_pending_ingest(conn, issuer=card.issuer) and gmail.is_connected():
        console.print("Trying the statements that are still waiting…")
        return _reprocess(conn, issuer=card.issuer, limit=ingest.REPROCESS_BATCH)
    return 0


def cmd_cards_list(args) -> int:
    conn = db.connect()
    db.init(conn)
    cards = db.list_cards(conn)
    if not cards:
        console.print("No cards yet. Add one with `carddues cards add`.")
        return 0
    table = Table(title="Cards")
    for column in ("ID", "Label", "Issuer", "Last 4", "Limit", "Unlock details"):
        table.add_column(column)
    for card in cards:
        details = []
        if card.name:
            details.append("name")
        if card.dob:
            details.append("dob")
        if card.pan:
            details.append("pan")
        if card.extra_passwords:
            details.append(f"{len(card.extra_passwords)} password(s)")
        table.add_row(
            str(card.id),
            card.label,
            issuers.get(card.issuer).name,
            card.last4,
            format_inr(card.credit_limit),
            ", ".join(details) or "—",
        )
    console.print(table)
    return 0


def cmd_cards_remove(args) -> int:
    conn = db.connect()
    card = _select_card(conn, args.card)
    db.delete_card(conn, card.id)
    console.print(f"Removed {card.label}.")
    return 0


def cmd_ingest(args) -> int:
    conn = db.connect()
    db.init(conn)
    try:
        summary = ingest.ingest_gmail(
            conn,
            lookback_days=args.days,
            limit=args.limit,
            keep_files=args.keep_files,
        )
    except gmail.GmailNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.print(
        f"Parsed [green]{summary.parsed}[/green] statement(s) from "
        f"{len(summary.results)} attachment(s)."
    )
    for result in summary.needs_attention:
        console.print(f"  [yellow]{result.status}[/yellow] {result.filename}: {result.detail}")
    if summary.needs_attention:
        console.print(
            "[dim]Locked files usually mean the card's name/date of birth is missing. "
            "Add them with `carddues cards add`, or enter the amount with `carddues set`.[/dim]"
        )
    return cmd_show(args) if args.show else 0


def cmd_import(args) -> int:
    conn = db.connect()
    db.init(conn)
    path = Path(args.path)
    result = ingest.ingest_pdf(
        conn,
        path,
        issuer_hint=args.issuer,
        source_ref=str(path.resolve()),
        extra_passwords=list(args.password or []),
    )
    db.log_ingest(
        conn,
        message_id=None,
        filename=path.name,
        status=result.status,
        detail=result.detail,
    )
    style = "green" if result.status == ingest.STATUS_PARSED else "yellow"
    console.print(f"[{style}]{result.status}[/{style}]: {result.detail}")
    return 0 if result.status == ingest.STATUS_PARSED else 1


def cmd_reprocess(args) -> int:
    conn = db.connect()
    db.init(conn)
    return _reprocess(conn, issuer=args.issuer, limit=args.limit)


def _reprocess(conn, *, issuer: str | None, limit: int) -> int:
    """Try the waiting attachments again with the cards as they stand now."""
    try:
        summary = ingest.reprocess_pending(conn, issuer=issuer, limit=limit)
    except gmail.GmailNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if not summary.results:
        console.print("Nothing is waiting to be opened.")
        return 0

    console.print(
        f"Opened [green]{summary.parsed}[/green] of {len(summary.results)} waiting attachment(s)."
    )
    for result in summary.needs_attention:
        console.print(f"  [yellow]{result.status}[/yellow] {result.filename}: {result.detail}")
    if summary.remaining:
        console.print(f"[dim]{summary.remaining} still waiting; run it again to continue.[/dim]")
    return 0


def cmd_reparse(args) -> int:
    """Re-read already-parsed statements so parser fixes rewrite stored dates."""
    conn = db.connect()
    db.init(conn)
    try:
        summary = ingest.reparse_parsed(conn, issuer=args.issuer, limit=args.limit)
    except gmail.GmailNotConfigured as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    if not summary.results:
        console.print("No parsed statements to re-read.")
        return 0

    console.print(
        f"Re-parsed [green]{summary.parsed}[/green] of {len(summary.results)} statement(s)."
    )
    for result in summary.needs_attention:
        console.print(f"  [yellow]{result.status}[/yellow] {result.filename}: {result.detail}")
    if summary.remaining and args.limit is not None:
        console.print(
            f"[dim]{summary.remaining} still to re-read; "
            "raise --limit or omit it to cover the rest.[/dim]"
        )
    return 0


def cmd_unlock(args) -> int:
    """Retry a locked attachment with a password you supply."""
    conn = db.connect()
    db.init(conn)
    locked = [
        row
        for row in db.unresolved_ingest(conn)
        if row["status"] == ingest.STATUS_LOCKED and row["message_id"]
    ]
    if not locked:
        console.print("Nothing is waiting on a password.")
        return 0

    if args.file:
        chosen = [row for row in locked if row["filename"] == args.file]
        if not chosen:
            console.print(f"[red]No locked attachment named {args.file}.[/red]")
            return 1
    else:
        chosen = locked
        if len(chosen) > 1:
            console.print("Several attachments are locked; naming one with --file is safer.")

    failures = 0
    for row in chosen:
        result = ingest.retry_locked(
            conn,
            message_id=row["message_id"],
            filename=row["filename"],
            password=args.password,
        )
        style = "green" if result.status == ingest.STATUS_PARSED else "yellow"
        console.print(f"{row['filename']}: [{style}]{result.status}[/{style}] {result.detail}")
        failures += result.status != ingest.STATUS_PARSED
    return 1 if failures else 0


def cmd_set(args) -> int:
    """Manual entry: the override for anything the parser could not read."""
    conn = db.connect()
    db.init(conn)
    card = _select_card(conn, args.card)
    statement_date = _parse_date(args.statement_date) or date.today()
    record = StatementRecord(
        card_id=card.id,
        total_due=args.total,
        min_due=args.min,
        due_date=_parse_date(args.due),
        statement_date=statement_date,
        credit_limit=args.limit,
        source=SOURCE_MANUAL,
        source_ref=None,
        as_of=datetime.now(),
        note=args.note,
        confidence=1.0,
    )
    db.save_statement(conn, record)
    console.print(
        f"[green]Set {card.label}: total due {format_inr(args.total)}"
        f"{', due ' + args.due if args.due else ''}.[/green]"
    )
    return 0


def cmd_paid(args) -> int:
    conn = db.connect()
    db.init(conn)
    card = _select_card(conn, args.card)
    view = dues.view_for_card(conn, card)
    amount = args.amount if args.amount is not None else (view.outstanding or 0.0)
    if amount <= 0:
        console.print("Nothing outstanding to record.")
        return 0
    db.add_payment(conn, card.id, amount, _parse_date(args.on) or date.today(), args.note)
    updated = dues.view_for_card(conn, card)
    console.print(
        f"[green]Recorded {format_inr(amount)} against {card.label}.[/green] "
        f"Remaining: {format_inr(updated.outstanding)}"
    )
    return 0


def _views_payload(views: list[dues.DueView]) -> list[dict]:
    return [
        {
            "card": view.card.label,
            "issuer": view.card.issuer,
            "last4": view.card.last4,
            "status": view.status,
            "billed_amount": view.billed_amount,
            "outstanding": view.outstanding,
            "min_due": view.min_due_remaining,
            "due_date": view.due_date.isoformat() if view.due_date else None,
            "statement_date": view.statement_date.isoformat() if view.statement_date else None,
            "days_to_due": view.days_to_due,
            "credit_limit": view.credit_limit,
            "available_credit": view.available_credit,
            "utilisation_percent": view.utilisation,
            "source": view.source,
            "as_of": view.as_of.isoformat() if view.as_of else None,
            "stale": view.is_stale,
            "note": view.freshness_note,
        }
        for view in views
    ]


def cmd_show(args) -> int:
    conn = db.connect()
    db.init(conn)
    book = dues.portfolio(conn)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "total_outstanding": book.total_outstanding,
                    "total_min_due": book.total_min_due,
                    "utilisation_percent": book.utilisation,
                    "cards": _views_payload(book.views),
                },
                indent=2,
            )
        )
        return 0

    if not book.views:
        console.print("No cards yet. Add one with `carddues cards add`.")
        return 0

    table = Table(title="Credit card dues", caption="Billed amounts, not live balances")
    table.add_column("Card", min_width=16, overflow="ellipsis")
    table.add_column("Status", no_wrap=True)
    table.add_column("Due date", no_wrap=True)
    table.add_column("Outstanding", justify="right", no_wrap=True)
    table.add_column("Min due", justify="right", no_wrap=True)
    table.add_column("Used", justify="right", no_wrap=True)
    table.add_column("As of", no_wrap=True)

    for view in book.views:
        days = view.days_to_due
        when = view.due_date.strftime("%d %b") if view.due_date else "—"
        if days is not None:
            when += f" ({days:+d}d)"
        table.add_row(
            view.card.label,
            f"[{STATUS_STYLE[view.status]}]{STATUS_TEXT[view.status]}[/]",
            when,
            format_inr(view.outstanding),
            format_inr(view.min_due_remaining),
            f"{view.utilisation}%" if view.utilisation is not None else "—",
            _freshness(view),
        )

    console.print(table)
    console.print(
        f"Total outstanding [bold]{format_inr(book.total_outstanding)}[/bold] · "
        f"minimum {format_inr(book.total_min_due)}"
        + (f" · utilisation {book.utilisation}%" if book.utilisation is not None else "")
    )
    return 0


def _freshness(view: dues.DueView) -> str:
    if not view.has_data:
        return "—"
    age = view.age_days
    label = f"{age}d ago" if age is not None else "unknown"
    if view.source == SOURCE_MANUAL:
        label += " (manual)"
    return f"[yellow]{label}[/yellow]" if view.is_stale else label


def cmd_log(args) -> int:
    conn = db.connect()
    db.init(conn)
    rows = db.recent_ingest_log(conn, limit=args.limit)
    if not rows:
        console.print("Nothing ingested yet.")
        return 0
    table = Table(title="Recent ingests")
    for column in ("When", "File", "Status", "Detail"):
        table.add_column(column)
    for row in rows:
        table.add_row(row["created_at"], row["filename"] or "—", row["status"], row["detail"] or "")
    console.print(table)
    return 0


def cmd_notify(args) -> int:
    """Send a macOS notification when something needs paying."""
    conn = db.connect()
    db.init(conn)
    book = dues.portfolio(conn)
    urgent = [v for v in book.attention if v.status != dues.STATUS_SETTLED]
    if not urgent:
        return 0

    lines = []
    for view in urgent[:4]:
        days = view.days_to_due
        when = "overdue" if days is not None and days < 0 else f"in {days}d" if days else "today"
        lines.append(f"{view.card.label}: {format_inr(view.outstanding)} {when}")
    body = " · ".join(lines).replace('"', "'")
    title = f"{len(urgent)} card(s) need attention"

    subprocess.run(
        ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
        check=False,
    )
    console.print(body)
    return 0


def cmd_serve(args) -> int:
    from .web.app import create_app

    app = create_app()
    console.print(f"Dashboard on [bold]http://{args.host}:{args.port}[/bold]")
    if not gmail.is_connected():
        console.print("Gmail is not connected yet — use Connect Gmail on that page.")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="carddues", description="Track Indian credit card dues")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the local database").set_defaults(func=cmd_init)
    sub.add_parser("auth", help="connect Gmail (read-only)").set_defaults(func=cmd_auth)

    audit_cmd = sub.add_parser(
        "audit", help="check every card for missing dues or transactions"
    )
    audit_cmd.set_defaults(func=cmd_audit)

    dump_golden = sub.add_parser(
        "dump-golden",
        help="write statement text dumps + expected stubs for golden tests",
    )
    dump_golden.add_argument(
        "--out",
        default=str(Path("tests/golden")),
        help="directory for .txt / .expected.json pairs",
    )
    dump_golden.set_defaults(func=cmd_dump_golden)

    cards = sub.add_parser("cards", help="manage cards").add_subparsers(
        dest="cards_command", required=True
    )

    add = cards.add_parser("add", help="register a card")
    add.add_argument("--issuer", required=True, choices=sorted(issuers.ISSUERS))
    add.add_argument("--last4", required=True)
    add.add_argument("--label")
    add.add_argument("--limit", type=float, help="credit limit")
    add.add_argument("--name", help="name as printed on the statement, for PDF passwords")
    add.add_argument("--dob", help="date of birth as dd/mm/yyyy, for PDF passwords")
    add.add_argument("--pan")
    add.add_argument("--password", action="append", help="known statement password (repeatable)")
    add.set_defaults(func=cmd_cards_add)

    cards.add_parser("list", help="list cards").set_defaults(func=cmd_cards_list)

    remove = cards.add_parser("remove", help="delete a card and its history")
    remove.add_argument("card")
    remove.set_defaults(func=cmd_cards_remove)

    ingest_cmd = sub.add_parser("ingest", help="fetch and parse statements from Gmail")
    ingest_cmd.add_argument("--days", type=int, default=config.DEFAULT_LOOKBACK_DAYS)
    ingest_cmd.add_argument("--limit", type=int, default=100, help="max emails to scan")
    ingest_cmd.add_argument("--keep-files", action="store_true", help="keep downloaded PDFs")
    ingest_cmd.add_argument("--show", action="store_true", help="print dues afterwards")
    ingest_cmd.set_defaults(func=cmd_ingest, json=False)

    import_cmd = sub.add_parser("import", help="parse a statement PDF from disk")
    import_cmd.add_argument("path")
    import_cmd.add_argument("--issuer", choices=sorted(issuers.ISSUERS))
    import_cmd.add_argument("--password", action="append")
    import_cmd.set_defaults(func=cmd_import)

    reprocess = sub.add_parser(
        "reprocess", help="retry the waiting attachments with the cards on file"
    )
    reprocess.add_argument("--issuer", choices=sorted(issuers.ISSUERS))
    reprocess.add_argument(
        "--limit", type=int, default=ingest.REPROCESS_BATCH, help="attachments per pass"
    )
    reprocess.set_defaults(func=cmd_reprocess)

    reparse = sub.add_parser(
        "reparse", help="re-read already-parsed statements with the current parsers"
    )
    reparse.add_argument("--issuer", choices=sorted(issuers.ISSUERS))
    reparse.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max attachments this pass (default: all)",
    )
    reparse.set_defaults(func=cmd_reparse)

    unlock = sub.add_parser("unlock", help="retry a locked statement with a password")
    unlock.add_argument("password")
    unlock.add_argument("--file", help="the attachment name, when several are locked")
    unlock.set_defaults(func=cmd_unlock)

    set_cmd = sub.add_parser("set", help="enter or correct a card's dues by hand")
    set_cmd.add_argument("card", help="card id, last 4 digits, or part of the label")
    set_cmd.add_argument("--total", type=float, required=True, help="total amount due")
    set_cmd.add_argument("--min", type=float, help="minimum amount due")
    set_cmd.add_argument("--due", help="payment due date as dd/mm/yyyy")
    set_cmd.add_argument("--statement-date", help="dd/mm/yyyy, defaults to today")
    set_cmd.add_argument("--limit", type=float, help="credit limit")
    set_cmd.add_argument("--note")
    set_cmd.set_defaults(func=cmd_set)

    paid = sub.add_parser("paid", help="record a payment")
    paid.add_argument("card")
    paid.add_argument("--amount", type=float, help="defaults to the full outstanding")
    paid.add_argument("--on", help="payment date as dd/mm/yyyy, defaults to today")
    paid.add_argument("--note")
    paid.set_defaults(func=cmd_paid)

    show = sub.add_parser("show", help="print current dues")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    log_cmd = sub.add_parser("log", help="show recent ingest results")
    log_cmd.add_argument("--limit", type=int, default=25)
    log_cmd.set_defaults(func=cmd_log)

    sub.add_parser("notify", help="macOS notification for anything due").set_defaults(
        func=cmd_notify
    )

    serve = sub.add_parser("serve", help="run the local dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--debug", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
