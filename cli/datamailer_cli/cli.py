"""Command-line interface for sending email through a Datamailer deployment."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import __version__, config
from .api import ApiError, DatamailerClient
from .templates import template_for


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except ApiError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datamailer",
        description="Send email through a Datamailer deployment from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"datamailer {__version__}")

    sub = parser.add_subparsers(dest="command")

    # --- configure --------------------------------------------------------
    p_configure = sub.add_parser("configure", help="Save the deployment URL and API key to the config file.")
    p_configure.add_argument("--url", help="Datamailer base URL, e.g. https://datamailer.example.com")
    p_configure.add_argument("--api-key", dest="api_key", help="Client API key (dm_...).")
    p_configure.add_argument("--default-to", dest="default_to", help="Default recipient for `datamailer send`.")
    p_configure.add_argument("--default-from", dest="default_from", help="Default sender id for `datamailer send`.")
    p_configure.set_defaults(handler=cmd_configure)

    # --- send -------------------------------------------------------------
    p_send = sub.add_parser("send", help="Send an email. Body may come from --body, --body-file, or stdin.")
    _add_connection_args(p_send)
    p_send.add_argument("--to", help="Recipient email (defaults to the configured default_to).")
    p_send.add_argument("--subject", "-s", help="Subject line.")
    p_send.add_argument("--body", "-b", help="Message body. If omitted, read from --body-file or stdin.")
    p_send.add_argument("--body-file", dest="body_file", help="Read the body from this file ('-' for stdin).")
    p_send.add_argument("--html", action="store_true", help="Treat the body as raw HTML instead of plain text.")
    p_send.add_argument("--from", dest="from_", help="Sender id (defaults to the client's default sender).")
    p_send.add_argument("--idempotency-key", dest="idempotency_key", help="Dedupe key; replays return the prior send.")
    p_send.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_send.set_defaults(handler=cmd_send)

    # --- status -----------------------------------------------------------
    p_status = sub.add_parser("status", help="Show delivery status for a transactional message id.")
    _add_connection_args(p_status)
    p_status.add_argument("message_id", help="Message id returned by `datamailer send`.")
    p_status.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_status.set_defaults(handler=cmd_status)

    # --- whoami -----------------------------------------------------------
    p_whoami = sub.add_parser("whoami", help="Verify the URL + token and show the authenticated client.")
    _add_connection_args(p_whoami)
    p_whoami.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_whoami.set_defaults(handler=cmd_whoami)

    # --- senders ----------------------------------------------------------
    p_senders = sub.add_parser("senders", help="Show or set the client's sender addresses.")
    _add_connection_args(p_senders)
    p_senders.add_argument(
        "--set",
        dest="set_senders",
        action="append",
        metavar="id=email",
        help="Define a sender, e.g. --set 'results=Agent <agent@example.com>'. Repeatable.",
    )
    p_senders.add_argument("--delete", action="append", default=[], help="Remove a sender id.")
    p_senders.add_argument("--default", dest="default_sender", help="Sender id to use as the default.")
    p_senders.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_senders.set_defaults(handler=cmd_senders)

    routing = sub.add_parser("inbound", help="Manage domain routing with an admin API key.")
    _add_connection_args(routing)
    routing.add_argument("resource", choices=("domains", "mailboxes", "deliveries", "dmarc"))
    routing.add_argument("--id", type=int)
    routing.add_argument("--data-file", help="Create or patch from a JSON file; use - for stdin.")
    routing.set_defaults(handler=cmd_inbound)
    domains = sub.add_parser("domains", help="Manage sending domains through Relay.")
    _add_connection_args(domains)
    domains.add_argument("--admin", action="store_true", help="Use an admin key to authorize domains and provider routes.")
    domains.add_argument("--id", type=int)
    domains.add_argument("--data-file", help="Register/update a domain from JSON; - reads stdin.")
    domains.add_argument("--verify", action="store_true")
    domains.add_argument("--delete", action="store_true", help="Disable a domain.")
    domains.set_defaults(handler=cmd_domains)

    p_layout = sub.add_parser("layout", help="Show or set the client's email layout (own header/footer/shell).")
    _add_connection_args(p_layout)
    p_layout.add_argument("--set-html", dest="set_html", help="Set layout_html from this string.")
    p_layout.add_argument("--set-file", dest="set_file", help="Set layout_html from this file.")
    p_layout.add_argument("--brand", dest="brand_name", help="Brand display name for the layout.")
    p_layout.add_argument("--website", dest="website_url", help="Brand website URL.")
    p_layout.add_argument("--logo", dest="logo_url", help="Brand logo URL.")
    p_layout.add_argument("--sender", dest="sender_id", default="", help="Per-sender override id.")
    p_layout.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_layout.set_defaults(handler=cmd_layout)
    return parser


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="Override the configured Datamailer base URL.")
    parser.add_argument("--api-key", dest="api_key", help="Override the configured API key.")


def _client(args) -> DatamailerClient:
    settings = config.resolve(args)
    return DatamailerClient(settings.base_url, settings.api_key)


# --- command handlers -----------------------------------------------------


def cmd_configure(args) -> int:
    url = args.url or _prompt("Datamailer URL", config.resolve(args).url)
    api_key = args.api_key or _prompt("API key (dm_...)", "", secret=True)
    values = {"url": url, "api_key": api_key}
    if args.default_to is not None:
        values["default_to"] = args.default_to
    if args.default_from is not None:
        values["default_from"] = args.default_from

    path = config.save_file(values)
    print(f"Saved configuration to {path}")
    return 0


def cmd_send(args) -> int:
    settings = config.resolve(args)
    client = DatamailerClient(settings.base_url, settings.api_key)

    recipient = args.to or settings.default_to
    if not recipient:
        raise ApiError("No recipient. Pass --to or set a default with `datamailer configure --default-to`.")
    if not args.subject:
        raise ApiError("Missing --subject.")

    body = _read_body(args)
    if not body:
        raise ApiError("Empty body. Pass --body, --body-file, or pipe text on stdin.")

    template_key, template = template_for(args.html)
    payload = {
        "email": recipient,
        "template_key": template_key,
        "context": {"subject": args.subject, "body": body},
    }
    from_sender = args.from_ or settings.default_from
    if from_sender:
        payload["from_email"] = from_sender
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key

    try:
        result = client.send(payload)
    except ApiError as exc:
        if not exc.is_template_not_found:
            raise
        # First use against this deployment: provision the generic template, then retry once.
        client.upsert_template(template_key, template)
        result = client.send(payload)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    message = result.get("message", {}) if isinstance(result, dict) else {}
    print(f"Sent to {message.get('email', recipient)}")
    print(f"  message id: {message.get('id')}")
    print(f"  status:     {message.get('status')}")
    if result.get("idempotent_replay"):
        print("  (idempotent replay of an earlier send)")
    return 0


def cmd_status(args) -> int:
    result = _client(args).message_status(args.message_id)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    message = result.get("message", {}) if isinstance(result, dict) else {}
    print(f"message {message.get('id')}: {message.get('status')}")
    print(f"  to:      {message.get('email')}")
    print(f"  subject: {message.get('subject')}")
    if message.get("ses_message_id"):
        print(f"  ses id:  {message.get('ses_message_id')}")
    events = result.get("events", []) if isinstance(result, dict) else []
    for event in events:
        print(f"  - {event.get('created_at')}  {event.get('event_type')}")
    return 0


def cmd_whoami(args) -> int:
    result = _client(args).whoami()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    client_info = result.get("client", {}) if isinstance(result, dict) else {}
    print(f"client:  {client_info.get('name')} ({client_info.get('organization')}/{client_info.get('slug')})")
    print(f"default sender: {result.get('default_sender_id') or '(none configured)'}")
    senders = result.get("senders") or []
    if senders:
        print("senders:")
        for sender in senders:
            print(f"  - {sender.get('id')}: {sender.get('email')}")
    return 0


def cmd_senders(args) -> int:
    client = _client(args)
    if not args.set_senders and not args.default_sender and not args.delete:
        return _print_senders(client.get_senders(), args.json)
    current = client.get_senders()
    senders = {item['id']: item for item in current.get('senders', [])}
    for item in args.set_senders or []:
        sender_id, sep, email = item.partition("=")
        if not sep or not sender_id.strip() or not email.strip():
            raise ApiError("Use --set id=email.")
        senders[sender_id.strip()] = {"id": sender_id.strip(), "email": email.strip()}
    for sender_id in args.delete:
        if sender_id not in senders:
            raise ApiError(f"Unknown sender: {sender_id}")
        del senders[sender_id]
    default = args.default_sender or current.get('default_sender_id') or next(iter(senders), '')
    if default not in senders:
        raise ApiError("Select a remaining sender with --default before deleting the default.")
    result = client.set_senders({'senders': list(senders.values()), 'default_sender_id': default})
    return _print_senders(result, args.json)


def _print_senders(result, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"default sender: {result.get('default_sender_id') or '(none configured)'}")
    for sender in result.get("senders") or []:
        print(f"  - {sender.get('id')}: {sender.get('email')}")
    return 0


def cmd_layout(args) -> int:
    client = _client(args)
    if not args.set_html and not args.set_file and not args.brand_name:
        result = client.get_layouts()
        if args.json:
            print(json.dumps(result, indent=2))
            return 0
        for item in result.get("layouts", []):
            print(f"{item.get('sender_id') or 'default'}: {item.get('brand_name')}")
        return 0
    layout_html = args.set_html or ""
    if args.set_file:
        with open(args.set_file, encoding="utf-8") as handle:
            layout_html = handle.read()
    payload = {
        "sender_id": args.sender_id or "",
        "brand_name": args.brand_name or "",
        "website_url": args.website_url or "",
        "logo_url": args.logo_url or "",
        "layout_html": layout_html,
    }
    result = client.set_layout(payload)
    print(json.dumps(result, indent=2) if args.json else f"layout saved: {result.get('brand_name')}")
    return 0


# --- helpers --------------------------------------------------------------


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.read()
        with open(args.body_file, encoding="utf-8") as handle:
            return handle.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _prompt(label: str, default: str, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    if secret:
        value = getpass.getpass(f"{label}: ")
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


def cmd_inbound(args):
    path = "/api/admin/inbound-" + args.resource
    if args.id is not None:
        path += f"/{args.id}"
    payload = None
    method = "GET"
    if args.data_file:
        if args.resource in {"deliveries", "dmarc"}:
            raise ApiError("Deliveries and DMARC archives are read-only.")
        try:
            if args.data_file == "-":
                payload = json.load(sys.stdin)
            else:
                with open(args.data_file) as stream:
                    payload = json.load(stream)
        except (OSError, ValueError):
            raise ApiError("Could not read routing JSON.") from None
        method = "PATCH" if args.id is not None else "POST"
    _, result = _client(args).request(method, path, payload=payload)
    print(json.dumps(result, indent=2))
    return 0


def cmd_domains(args):
    if not args.admin and (args.id or args.data_file or args.verify or args.delete):
        raise ApiError("Domain authorization requires --admin and an admin API key.")
    if args.verify and args.delete or args.data_file and (args.verify or args.delete):
        raise ApiError("Choose one domain action.")
    path = '/api/admin/sending-domains' if args.admin else '/api/client/sending-domains'
    if args.id:
        path += f'/{args.id}'
    method, payload = 'GET', None
    if args.verify or args.delete:
        if not args.id:
            raise ApiError('--id is required.')
        method = 'POST' if args.verify else 'DELETE'
        if args.verify:
            path += '/verify'
    if args.data_file:
        try:
            if args.data_file == '-':
                payload = json.load(sys.stdin)
            else:
                with open(args.data_file) as stream:
                    payload = json.load(stream)
        except (OSError, ValueError):
            raise ApiError('Could not read domain JSON.') from None
        method = 'PATCH' if args.id else 'POST'
    _, result = _client(args).request(method, path, payload=payload)
    print(json.dumps(result, indent=2))
    return 0
