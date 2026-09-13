#!/usr/bin/env python3
"""Install this skill as safe, repeatable Claude and/or Codex symlinks."""

import argparse
import os
import sys
from pathlib import Path


SKILL = Path(__file__).resolve().parents[1]


def fail(message):
    raise ValueError(message)


def client_home(client, target_home):
    if target_home is not None:
        return target_home / (".claude" if client == "claude" else ".codex")
    if client == "codex" and os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    return Path.home() / (".claude" if client == "claude" else ".codex")


def destinations(client, target_home):
    clients = ("claude", "codex") if client == "both" else (client,)
    return [(name, client_home(name, target_home) / "skills" / "superarmanda") for name in clients]


def existing_parent_is_safe(destination):
    parent = destination.parent
    while not parent.exists() and not parent.is_symlink():
        parent = parent.parent
    if parent.is_symlink() or not parent.is_dir():
        fail(f"destination parent is not a directory: {parent}")


def preflight(destination):
    existing_parent_is_safe(destination)
    if destination.is_symlink():
        if not destination.exists():
            fail(f"refusing dangling symlink: {destination}")
        if destination.resolve() != SKILL.resolve():
            fail(f"refusing foreign symlink: {destination}")
        return "present"
    if destination.exists():
        kind = "directory" if destination.is_dir() else "file"
        fail(f"refusing existing {kind}: {destination}")
    return "new"


def install(args):
    if not (SKILL / "SKILL.md").is_file():
        fail(f"skill source has no SKILL.md: {SKILL}")
    target_home = Path(args.target_home).expanduser() if args.target_home else None
    targets = destinations(args.client, target_home)
    # Check every target before making a directory or link. This keeps `both`
    # atomic with respect to known conflicts.
    states = [(name, destination, preflight(destination)) for name, destination in targets]
    for name, destination, state in states:
        if state == "present":
            print(f"{name}: already installed at {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(SKILL, destination, target_is_directory=True)
        print(f"{name}: installed at {destination}")


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(required=True)
    command = sub.add_parser("install")
    command.add_argument("--client", choices=("claude", "codex", "both"), required=True)
    command.add_argument(
        "--target-home",
        help="explicit home root for isolated installation; does not alter HOME",
    )
    command.set_defaults(func=install)
    return value


if __name__ == "__main__":
    try:
        arguments = parser().parse_args()
        arguments.func(arguments)
    except (OSError, ValueError) as error:
        print(f"install-skill: {error}", file=sys.stderr)
        raise SystemExit(2)
