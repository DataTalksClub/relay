"""Command-line interface for Relay operator and client APIs."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.parse

from . import __version__, auth, config
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
    except auth.LoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="Manage Relay campaigns, contacts, templates, and sends from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"relay {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_auth = sub.add_parser("auth", help="Log in to Relay with the shared OAuth device flow.")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_login = auth_sub.add_parser("login", help="Pair this CLI with Relay.")
    p_login.add_argument("--url", help="Relay base URL.")
    p_login.add_argument("--timeout", type=int, default=900, help="Seconds to wait for browser approval (default: 900).")
    p_login.set_defaults(handler=cmd_auth_login)
    auth_sub.add_parser("logout", help="Forget the saved Relay credential.").set_defaults(handler=cmd_auth_logout)
    auth_sub.add_parser("status", help="Show the configured Relay URL and login state.").set_defaults(handler=cmd_auth_status)

    p_api = sub.add_parser("api", help="Call a Relay API endpoint directly (useful for less common operator actions).")
    _add_connection_args(p_api)
    p_api.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    p_api.add_argument("path", help="Path beginning with /api/, for example /api/admin/dashboard.")
    p_api.add_argument("--data-file", help="JSON request body; use - to read stdin.")
    p_api.add_argument("--json", action="store_true", help="Print JSON (the default).")
    p_api.set_defaults(handler=cmd_api)

    p_operator = sub.add_parser("operator", help="Run a supported Relay operator action through the admin API.")
    _add_connection_args(p_operator)
    p_operator.add_argument("resource", choices=("campaigns", "contacts", "templates", "lists", "newsletters", "messages", "stats", "complaints", "archive"))
    p_operator.add_argument("action", choices=("list", "show", "create", "edit", "delete", "stats", "queue", "cancel", "pause", "resume", "stop", "recipients", "recipient-retry", "assume-sent", "unknown-recipients", "unknown-resend", "state", "subscriptions", "tag-add", "tag-remove", "preferences", "erase", "history", "validation", "suppression", "verification", "import", "tags", "tag-create", "tag-edit", "recipient-list", "recipient-list-upsert", "members", "member-upsert", "member-remove", "members-upsert", "members-reconcile", "import-show", "list-send", "retry", "bulk-retry", "versions", "publish", "block-types", "block-add", "block-show", "block-edit", "block-delete", "preview", "test-send", "export"))
    p_operator.add_argument("--id", type=int, help="Record id for show or an action.")
    p_operator.add_argument("--client-id", type=int, help="Explicit Relay client id for scoped operations.")
    p_operator.add_argument("--query", help="Text search for list actions that support it.")
    p_operator.add_argument("--suppression", choices=("global_unsubscribed", "hard_bounced", "complained"), help="Filter contacts by suppression state.")
    p_operator.add_argument("--client-slug", help="Client slug required by some campaign API operations.")
    p_operator.add_argument("--audience", help="Audience slug required by some campaign API operations.")
    p_operator.add_argument("--key", help="Newsletter issue key for issue actions.")
    p_operator.add_argument("--block-id", type=int, help="Newsletter block id.")
    p_operator.add_argument("--recipient-id", type=int, help="Campaign recipient id.")
    p_operator.add_argument("--audience-id", type=int, help="Audience id for tag listing.")
    p_operator.add_argument("--source-key", help="External source object key for a recipient list member.")
    p_operator.add_argument("--job-id", type=int, help="Recipient list import job id.")
    p_operator.add_argument("--data-file", help="JSON request body; use - to read stdin.")
    p_operator.add_argument("--resource-name", help="Archive resource name for paged exports.")
    p_operator.add_argument("--offset", type=int, default=0, help="Archive export page offset.")
    p_operator.add_argument("--limit", type=int, default=200, help="Archive export page size.")
    p_operator.add_argument("--include-removed", action="store_true", help="Include removed recipient-list members.")
    p_operator.add_argument("--confirm", action="store_true", help="Confirm that Relay should queue/send the campaign.")
    p_operator.add_argument("--json", action="store_true", help="Print JSON (the default).")
    p_operator.set_defaults(handler=cmd_operator)

    # --- configure --------------------------------------------------------
    p_configure = sub.add_parser("configure", help="Save the Relay URL and client API key to the config file.")
    p_configure.add_argument("--url", help="Relay base URL, e.g. https://relay.example.com")
    p_configure.add_argument("--api-key", dest="api_key", help="Relay client API key (relay_...).")
    p_configure.add_argument("--default-to", dest="default_to", help="Default recipient for `relay send`.")
    p_configure.add_argument("--default-from", dest="default_from", help="Default sender id for `relay send`.")
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
    p_status.add_argument("message_id", help="Message id returned by `relay send`.")
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
    parser.add_argument("--url", help="Override the configured Relay base URL.")
    parser.add_argument("--api-key", dest="api_key", help="Override the configured API key.")


def _client(args) -> DatamailerClient:
    settings = config.resolve(args)
    return DatamailerClient(settings.base_url, settings.api_key)


# --- command handlers -----------------------------------------------------


def cmd_auth_login(args) -> int:
    settings = config.resolve(args)
    url = (getattr(args, "url", None) or settings.base_url).rstrip("/")
    if not url:
        raise auth.LoginError("No Relay URL configured. Pass --url or set RELAY_URL.")
    if args.timeout <= 0:
        raise auth.LoginError("--timeout must be a positive number of seconds.")
    auth.login(url, timeout=args.timeout)
    print(f"Logged in to {url}; credential saved to {config.config_path()}.")
    return 0


def cmd_auth_logout(_args) -> int:
    revoked = auth.logout()
    if revoked:
        print("Logged out. The saved API credential was removed.")
    else:
        print("Local credential removed; Relay could not revoke the admin key. Revoke it in Relay admin settings.")
    return 0


def cmd_auth_status(_args) -> int:
    settings = config.resolve(_args)
    print(f"Relay URL: {settings.base_url or '(not configured)'}")
    print(f"Login: {'configured' if settings.api_key else 'not logged in'}")
    return 0


def cmd_api(args) -> int:
    if (
        not args.path.startswith("/api/")
        or ".." in args.path.split("/")
        or any(character in args.path for character in "?#\r\n")
    ):
        raise ApiError("API path must begin with /api/ and cannot contain '..'.")
    payload = None
    if args.data_file:
        try:
            if args.data_file == "-":
                if sys.stdin.isatty():
                    raise ApiError("No JSON was piped to stdin; pass a file path or pipe a JSON object.")
                payload = json.load(sys.stdin)
            else:
                with open(args.data_file, encoding="utf-8") as handle:
                    payload = json.load(handle)
        except (OSError, ValueError):
            raise ApiError("Could not read request JSON.") from None
    if payload is not None and not isinstance(payload, dict):
        raise ApiError("Request JSON must be an object.")

    _, result = _client(args).request(args.method, args.path, payload=payload)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_operator(args) -> int:
    resource, action = args.resource, args.action
    query = None
    method = "GET"
    payload = None
    if args.data_file:
        try:
            if args.data_file == "-":
                if sys.stdin.isatty():
                    raise ApiError("No JSON was piped to stdin; pass a file path or pipe a JSON object.")
                payload = json.load(sys.stdin)
            else:
                with open(args.data_file, encoding="utf-8") as handle:
                    payload = json.load(handle)
        except (OSError, ValueError):
            raise ApiError("Could not read request JSON.") from None
    if payload is not None and not isinstance(payload, dict):
        raise ApiError("Request JSON must be an object.")

    issue_key = urllib.parse.quote(args.key or "", safe="")

    if resource == "campaigns":
        if action == "list":
            path = "/api/admin/campaigns"
        elif action == "show" and args.id:
            path = f"/api/admin/campaigns/{args.id}"
        elif action == "stats" and args.id:
            path = f"/api/admin/campaigns/{args.id}/overview"
        elif action in {"create", "edit"} and args.client_id and (action == "create" or args.id):
            path = f"/api/admin/clients/{args.client_id}/campaign-drafts"
            if args.id:
                path += f"/{args.id}"
            method = "POST" if action == "create" else "PATCH"
        elif action == "queue" and args.id:
            path = f"/api/admin/campaigns/{args.id}/queue"
            method = "POST"
            if not args.confirm:
                raise ApiError("Queueing sends email. Repeat with --confirm to queue this campaign.")
            payload = payload or {"confirm": True}
        elif action == "assume-sent" and args.id and args.recipient_id:
            path = f"/api/admin/campaigns/{args.id}/recipients/{args.recipient_id}/assume-sent"
            method = "POST"
            if not args.confirm:
                raise ApiError("Assume-sent changes campaign results. Repeat with --confirm.")
            payload = payload or {}
        elif action in {"cancel", "pause", "resume", "stop", "recipients", "recipient-retry", "unknown-recipients", "unknown-resend", "preview", "test-send"}:
            if not args.client_id or not args.key:
                raise ApiError(f"Campaign {action} requires --client-id and --key.")
            if not args.client_slug or not args.audience:
                raise ApiError(f"Campaign {action} requires --client-slug and --audience for API scope.")
            path = f"/api/admin/clients/{args.client_id}/campaigns/{issue_key}"
            if action in {"recipients", "unknown-recipients"}:
                path += "/" + ("recipients" if action == "recipients" else "unknown-recipients")
                query = {"client": args.client_slug, "audience": args.audience}
            elif action == "recipient-retry":
                if not args.recipient_id:
                    raise ApiError("Campaign recipient retry requires --recipient-id.")
                path += f"/recipients/{args.recipient_id}/retry"
                method = "POST"
            elif action == "unknown-resend":
                path += "/unknown-recipients/resend"
                method = "POST"
            elif action in {"preview", "test-send"}:
                path += "/" + action
                method = "POST"
            else:
                path += "/" + action
                method = "POST"
            if action in {"test-send", "recipient-retry", "unknown-resend"} and not args.confirm:
                raise ApiError(f"Campaign {action} can send email. Repeat with --confirm.")
            if action in {"cancel", "pause", "resume", "stop"} and not args.confirm:
                raise ApiError(f"Campaign {action} changes delivery state. Repeat with --confirm.")
            if method == "POST":
                payload = dict(payload or {})
                payload.setdefault("client", args.client_slug)
                payload.setdefault("audience", args.audience)
                if action == "preview":
                    payload.setdefault("block_id", args.block_id)
        else:
            raise ApiError("Campaign action needs a valid --id, --client-id, or supported action.")
    elif resource in {"contacts", "complaints"}:
        if action == "list":
            path = "/api/admin/contacts"
            if resource == "complaints":
                query = {"suppression": "complained"}
            elif args.query or args.suppression:
                query = {key: value for key, value in {"q": args.query, "suppression": args.suppression}.items() if value}
        elif action == "show" and args.id:
            path = f"/api/admin/contacts/{args.id}"
        elif action == "stats" and args.id:
            path = f"/api/admin/contacts/{args.id}/overview"
        elif action in {"history", "validation", "suppression", "verification"} and args.id and args.client_id:
            path = f"/api/admin/clients/{args.client_id}/contacts/{args.id}/{action}"
            if action in {"validation", "suppression", "verification"}:
                method = "PATCH"
        elif action == "export" and args.client_id:
            path = f"/api/admin/clients/{args.client_id}/contacts"
            query = {key: value for key, value in {"q": args.query, "suppression": args.suppression}.items() if value}
        elif action == "import" and args.client_id:
            path = f"/api/admin/clients/{args.client_id}/contacts/imports"
            method = "POST"
        elif action == "state" and args.id:
            path = f"/api/admin/contacts/{args.id}/state"
            method = "POST"
            if not args.confirm:
                raise ApiError("Changing contact state requires --confirm.")
        elif action in {"subscriptions", "tag-add", "tag-remove"} and args.id and args.client_id:
            suffix = {"subscriptions": "subscriptions", "tag-add": "tags/add", "tag-remove": "tags/remove"}[action]
            path = f"/api/admin/clients/{args.client_id}/contacts/{args.id}/{suffix}"
            method = "POST"
        elif action in {"preferences", "erase"} and args.client_id:
            path = f"/api/admin/clients/{args.client_id}/contacts/{action}"
            method = "PUT" if action == "preferences" else "POST"
            if action == "erase" and not args.confirm:
                raise ApiError("Contact erasure is irreversible. Repeat with --confirm.")
        else:
            raise ApiError("Contact action requires an appropriate --id and, for scoped operations, --client-id.")
    elif resource == "templates":
        if action == "list":
            path = "/api/admin/templates"
        elif action == "show" and args.id:
            path = f"/api/admin/templates/{args.id}"
        elif action == "create" and not args.key:
            path = "/api/admin/templates"
            method = "POST"
        elif action == "edit" and args.id:
            path = f"/api/admin/templates/{args.id}"
            method = "PATCH"
        elif action == "delete" and args.id:
            if not args.confirm:
                raise ApiError("Deleting a template is permanent. Repeat with --confirm.")
            path = f"/api/admin/templates/{args.id}"
            method = "DELETE"
        elif action in {"show", "create", "edit", "publish", "versions", "preview", "test-send"}:
            if not args.client_id or not args.key:
                raise ApiError(f"Transactional template {action} requires --client-id and --key.")
            path = f"/api/admin/clients/{args.client_id}/transactional/templates/{issue_key}"
            if action == "show":
                pass
            elif action == "create" or action == "edit":
                method = "PUT"
            elif action == "publish":
                path += "/publish"
                method = "POST"
                if not args.confirm:
                    raise ApiError("Publishing changes the active template. Repeat with --confirm.")
                payload = payload or {}
            elif action in {"versions", "preview", "test-send"}:
                path += "/" + ("versions" if action == "versions" else action)
                method = "GET" if action == "versions" else "POST"
                if action == "preview" and payload is None:
                    payload = {}
                if action == "test-send" and not args.confirm:
                    raise ApiError("Template test-send sends real email. Repeat with --confirm.")
        else:
            raise ApiError("Template actions support list/show and keyed transactional template operations.")
    elif resource == "newsletters":
        if action == "block-types":
            if not args.client_id:
                raise ApiError("Newsletter block-types requires --client-id.")
            path = f"/api/admin/clients/{args.client_id}/newsletter-block-types"
        else:
            if not args.client_id:
                raise ApiError("Newsletter actions require --client-id.")
            path = f"/api/admin/clients/{args.client_id}/newsletter-issues"
            if action == "list":
                pass
            elif not args.key:
                raise ApiError("Newsletter issue actions require --key.")
            elif action in {"show", "create", "edit"}:
                path += f"/{issue_key}"
                if action in {"create", "edit"}:
                    method = "PUT"
            elif action == "block-add":
                path += f"/{issue_key}/blocks"
                method = "POST"
            elif action in {"block-show", "block-edit", "block-delete"} and args.block_id:
                path += f"/{issue_key}/blocks/{args.block_id}"
                method = {"block-show": "GET", "block-edit": "PUT", "block-delete": "DELETE"}[action]
                if action == "block-delete" and not args.confirm:
                    raise ApiError("Deleting a newsletter block is permanent. Repeat with --confirm.")
            elif action in {"preview", "test-send"}:
                path += f"/{issue_key}/{action}"
                method = "POST"
                if action == "test-send" and not args.confirm:
                    raise ApiError("Newsletter test-send sends real email. Repeat with --confirm.")
                if action == "preview" and payload is None:
                    payload = {"block_id": args.block_id}
            else:
                raise ApiError("Unsupported newsletter action or missing --block-id.")
    elif resource == "messages":
        if action == "list":
            path = "/api/admin/transactional-messages"
        elif action == "show" and args.id:
            path = f"/api/admin/transactional-messages/{args.id}"
        elif action == "retry" and args.id:
            if not args.confirm:
                raise ApiError("Retrying may send email. Repeat with --confirm to retry this message.")
            path = f"/api/admin/transactional-messages/{args.id}/retry"
            method = "POST"
            payload = payload or {}
        elif action == "bulk-retry":
            path = "/api/admin/transactional-messages/retry"
            method = "POST"
            if payload is None:
                raise ApiError("Bulk retry requires --data-file with filters and dry_run/confirm.")
            if args.confirm:
                payload["confirm"] = True
            elif not payload.get("dry_run"):
                raise ApiError("Bulk retry can send email. Use dry_run in the JSON file or repeat with --confirm.")
        else:
            raise ApiError("Message actions support list, show --id, or retry --id --confirm.")
    elif resource == "lists":
        if action == "tags":
            if not args.audience_id:
                raise ApiError("List tag action requires --audience-id.")
            path = "/api/admin/tags"
            query = {"audience_id": args.audience_id}
        elif action == "tag-create":
            path = "/api/admin/tags"
            method = "POST"
        elif action == "tag-edit" and args.id:
            path = f"/api/admin/tags/{args.id}"
            method = "PATCH"
        elif action in {"recipient-list", "recipient-list-upsert", "members", "member-upsert", "member-remove", "members-upsert", "members-reconcile", "import", "import-show", "list-send"}:
            if not args.client_id or not args.key or not args.client_slug or not args.audience:
                raise ApiError(f"Recipient-list {action} requires --client-id, --client-slug, --audience, and --key.")
            path = f"/api/admin/clients/{args.client_id}/recipient-lists/{issue_key}"
            if action == "recipient-list":
                query = {"client": args.client_slug, "audience": args.audience}
            elif action == "recipient-list-upsert":
                method = "PUT"
            elif action == "members":
                path += "/members"
                query = {
                    "client": args.client_slug,
                    "audience": args.audience,
                    "limit": args.limit,
                    "include_removed": str(args.include_removed).lower(),
                }
            elif action == "member-upsert" or action == "member-remove":
                if not args.source_key:
                    raise ApiError(f"Recipient-list {action} requires --source-key.")
                path += f"/members/{urllib.parse.quote(args.source_key, safe='')}"
                method = "PUT" if action == "member-upsert" else "DELETE"
                if action == "member-remove" and not args.confirm:
                    raise ApiError("Removing a list member is permanent. Repeat with --confirm.")
            elif action == "members-upsert":
                path += "/members/bulk-upsert"
                method = "POST"
            elif action == "members-reconcile":
                path += "/members/reconcile"
                method = "POST"
                if not args.confirm:
                    raise ApiError("List reconciliation can remove absent members. Repeat with --confirm.")
            elif action == "import":
                path += "/imports"
                method = "POST"
            elif action == "import-show" and args.job_id:
                path += f"/imports/{args.job_id}"
                query = {"client": args.client_slug, "audience": args.audience}
            elif action == "list-send":
                path += "/transactional-send"
                method = "POST"
                if not args.confirm:
                    raise ApiError("Sending to a recipient list sends real email. Repeat with --confirm.")
            else:
                raise ApiError("Unsupported recipient-list action or missing --job-id.")
        elif action == "list":
            path = "/api/admin/audiences"
        elif action == "show" and args.id:
            path = f"/api/admin/audiences/{args.id}"
        elif action in {"create", "edit"} and (action == "create" or args.id):
            path = "/api/admin/audiences"
            if args.id:
                path += f"/{args.id}"
            method = "POST" if action == "create" else "PATCH"
        else:
            raise ApiError("List actions support list, show, create, and edit with --id.")
    elif resource == "stats":
        if action != "list":
            raise ApiError("Use `operator stats list` for the Relay dashboard summary.")
        path = "/api/admin/dashboard"
    else:  # archive
        if action != "export":
            raise ApiError("Archive supports export only.")
        path = "/api/admin/transfer/export"
        query = {"offset": args.offset, "limit": args.limit}
        if args.resource_name:
            query["resource"] = args.resource_name

    if method in {"POST", "PATCH"} and payload is None:
        raise ApiError("This action requires --data-file with a JSON object.")
    if method == "PUT" and payload is None:
        raise ApiError("This action requires --data-file with a JSON object.")
    if method in {"POST", "PATCH", "PUT"} and payload is not None and not isinstance(payload, dict):
        raise ApiError("Request JSON must be an object.")
    if resource == "lists" and action in {"recipient-list-upsert", "member-upsert", "member-remove", "members-upsert", "members-reconcile", "import", "list-send"}:
        if payload is None:
            raise ApiError("This action requires --data-file with a JSON object.")
        payload.setdefault("client", args.client_slug)
        payload.setdefault("audience", args.audience)
    _, result = _client(args).request(method, path, payload=payload, query=query)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_configure(args) -> int:
    url = args.url or _prompt("Relay URL", config.resolve(args).url)
    api_key = args.api_key or _prompt("Client API key (relay_...)", "", secret=True)
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
        raise ApiError("No recipient. Pass --to or set a default with `relay configure --default-to`.")
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
