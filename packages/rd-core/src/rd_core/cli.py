"""Common client configuration CLI, without importing devices or media runtimes."""

import argparse
from pathlib import Path

from .configuration import ConfigError, cli_overrides, load_config


def client_arguments(schema, role, argv=None):
    parser = argparse.ArgumentParser(description=f"Remote-driving {role} application")
    parser.add_argument(
        "--config", type=Path, help="instance YAML; otherwise RD_CONFIG"
    )
    parser.add_argument("--set", action="append", metavar="FIELD=VALUE")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-config",
        action="store_true",
        help="validate local credentials, TLS and dependencies without opening devices",
    )
    modes.add_argument(
        "--dump-config",
        action="store_true",
        help="print effective configuration and sources without resolving secrets",
    )
    parser.add_argument(
        "--session", type=int, help="control session (independent of room)"
    )
    parser.add_argument("--room", help="media room (independent of session)")
    if role == "driving":
        parser.add_argument(
            "--connect",
            action="store_true",
            help="connect immediately with the configured session and room",
        )
    else:
        parser.add_argument("--components", help="control,media or either component")
    args = parser.parse_args(argv)
    overrides = {
        name: getattr(args, name)
        for name in ("session", "room")
        if getattr(args, name) is not None
    }
    if role == "vehicle" and args.components is not None:
        overrides["components"] = args.components.split(",")
    try:
        explicit = cli_overrides(args.set)
        if set(explicit) & set(overrides):
            raise ConfigError(
                "do not set a field through both a convenience flag and --set"
            )
        args.loaded = load_config(
            schema, role, path=args.config, overrides={**overrides, **explicit}
        )
    except ConfigError as exc:
        parser.error(str(exc))
    return args
