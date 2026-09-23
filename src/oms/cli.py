"""Local Community command line. Every contribution uses the same coordinator."""
import argparse
import asyncio
from dataclasses import asdict, replace
import json
import sys
from pathlib import Path

from oms.settings.core import GIT_LOOPBACK_REFUSAL, CoreSettings, advertises_loopback


def main(argv=None):
    parser = argparse.ArgumentParser(prog="oms", description="Local skills, manual learning and publication")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("mcp")
    sub.add_parser("capabilities")
    sub.add_parser("review")
    imp = sub.add_parser("import")
    imp.add_argument("path", type=Path)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("correction")
    ingest.add_argument("--skill-hint")
    ingest.add_argument("--transaction-id")
    decision = sub.add_parser("decide")
    decision.add_argument("transaction_id")
    decision.add_argument("action", choices=["create", "reinforce", "reject", "release_safety"])
    decision.add_argument("--body", default="")
    decision.add_argument("--skill", action="append", default=[])
    decision.add_argument("--rule-id")
    publish = sub.add_parser("publish")
    publish.add_argument("--out", type=Path)
    publish.add_argument("--git-url", help="explicit destination repository for this manual publication")
    publish.add_argument("--branch", default="main")
    publish.add_argument("--dry-run", action="store_true")
    git_login = sub.add_parser("git-login", help="connect the Community publisher to an HTTPS Git repository")
    git_login.add_argument("--repository", required=True)
    git_login.add_argument("--check", action="store_true", help="check repository reads using existing Git access; do not prompt or save credentials")
    delete = sub.add_parser("delete-payload")
    delete.add_argument("transaction_id")
    args = parser.parse_args(argv)
    if args.command == "git-login":
        from oms.community.git_access import login
        return login(args.repository, check=args.check)
    if args.command == "publish" and args.out and args.git_url:
        parser.error("choose --out or --git-url")
    if args.command == "publish" and args.dry_run and not args.git_url:
        parser.error("--dry-run requires --git-url")
    settings = CoreSettings.from_env()
    if args.command == "serve":
        settings = replace(settings, host=args.host or settings.host).with_port(
            args.port or settings.port).validate()
    from oms.adapters.neo4j.driver import build_driver
    from oms.community.app import build_community
    driver = build_driver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    try:
        services = build_community(settings, driver)
        if args.command == "serve":
            import uvicorn
            from oms.web.api import create_app
            uvicorn.run(create_app(services), host=settings.host, port=settings.port)
            return 0
        if args.command == "mcp":
            asyncio.run(run_stdio(services))
            return 0
        if args.command == "capabilities":
            from oms.community.routes import build_routes
            from oms.web.capabilities import capabilities_for
            result = capabilities_for("community", services, build_routes(services))
        elif args.command == "import":
            method = services.importer.import_directory if args.path.is_dir() else services.importer.import_file
            result = asdict(method(args.path, settings.tenant_id))
        elif args.command == "ingest":
            from oms.ingestion.contribution import ContributionRequest, correction_payload
            from oms.domain.types import SourceRuntime
            principal = services.principal_resolver.resolve()
            payload = correction_payload(ContributionRequest(correction=args.correction,
                skill_hint=args.skill_hint, transaction_id=args.transaction_id), principal, SourceRuntime.MANUAL)
            result = asdict(services.coordinator.ingest(payload, principal))
        elif args.command == "review":
            result = services.manual.inbox(settings.tenant_id)
        elif args.command == "decide":
            from oms.community.workflow import Decision
            result = asdict(services.manual.decide(args.transaction_id, settings.tenant_id,
                services.principal_resolver.resolve().id, Decision(action=args.action,
                body=args.body, skill_ids=args.skill, rule_id=args.rule_id)))
        elif args.command == "delete-payload":
            txn = services.store.get_transaction(args.transaction_id)
            if txn is None or txn.tenant_id != settings.tenant_id:
                parser.error("transaction not found in this workspace")
            services.payloads.delete(txn.id)
            result = {"deleted": txn.id}
        else:
            if args.git_url:
                from oms.publish.git import GitCommandError, hold_checkout_lock, publish_to_repo
                if not args.dry_run and advertises_loopback(settings.public_url):
                    # The same refusal the console gives. A push is how a bundle
                    # reaches other machines; a dry run reaches none, so it is
                    # left alone.
                    print(f"Git publication refused: {GIT_LOOPBACK_REFUSAL}", file=sys.stderr)
                    return 1
                checkout = settings.data_dir / "git-publication"
                try:
                    with hold_checkout_lock(checkout):
                        gate, pushed = publish_to_repo(services.publisher, settings.tenant_id,
                            args.git_url, checkout, args.branch, args.dry_run)
                except GitCommandError:
                    # Git diagnostics can repeat credentials embedded in a
                    # remote URL. Keep the command-line refusal credential-free.
                    print("Git publication failed; check the remote and your Git access.", file=sys.stderr)
                    return 1
                result = {**asdict(gate), "pushed": pushed}
            else:
                result = asdict(services.publisher.publish(settings.tenant_id, args.out or settings.data_dir / "published"))
            if not result["passed"]:
                print(json.dumps(result))
                return 1
        print(json.dumps(result, default=str, indent=2))
        return 0
    finally:
        driver.close()


async def run_stdio(services):
    from mcp.server.stdio import stdio_server
    from oms.mcp_server.server import build_server
    server = build_server(services.coordinator,
        resolve_principal=services.principal_resolver, catalogue=services.catalogue)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    raise SystemExit(main())
