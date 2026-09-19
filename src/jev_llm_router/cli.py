"""Command-line entry point and no-generation diagnostics."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import httpx
import uvicorn

from .app import create_app, upstream_headers
from .config import load_config
from .routing import Router
from .state import AffinityStore


def main() -> None:
    parser = argparse.ArgumentParser(description="JEV Responses API model router")
    parser.add_argument("--config", help="TOML configuration path (or JEV_ROUTER_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Run the authenticated gateway (one worker)")
    doctor = commands.add_parser("doctor", help="Validate config and credentials")
    doctor.add_argument("--check-upstream", action="store_true", help="Check /models; never generate tokens")
    route = commands.add_parser("route", help="Preview rule-based routing locally, without credentials")
    route.add_argument("prompt")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "route":
            store = AffinityStore(":memory:")
            try:
                decision = Router(config, store).rules({"input": args.prompt})
                print(json.dumps(Router.describe(decision), indent=2))
            finally:
                store.close()
        elif args.command == "doctor":
            _, key = config.credentials()
            print("Configuration and credential presence: OK (secret values not printed)")
            if args.check_upstream:
                async def check():
                    async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                                 trust_env=config.upstream.trust_env) as client:
                        response = await client.get(config.upstream.base_url + "/models",
                                                    headers=upstream_headers(config, key))
                        response.raise_for_status()
                        available = {row["id"] for row in response.json().get("data", [])
                                     if isinstance(row, dict) and isinstance(row.get("id"), str)}
                        missing = [spec.id for spec in config.models.values() if spec.id not in available]
                        for tier, spec in config.models.items():
                            print(f"{tier}: {spec.id}: {'listed' if spec.id in available else 'NOT LISTED'}")
                        print("Listing is not a guarantee of generation permissions or supported effort.")
                        return 1 if missing else 0
                sys.exit(asyncio.run(check()))
        else:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
            # Client debug logs may include URLs. Never enable HTTPX request/body debugging here.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            uvicorn.run(create_app(config), host=config.server.host, port=config.server.port,
                        workers=1, access_log=False)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # Validation errors can contain configured values. Do not print exception payloads.
        print(f"Startup/diagnostic failed ({type(exc).__name__}); check TOML, credentials, and upstream access.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
