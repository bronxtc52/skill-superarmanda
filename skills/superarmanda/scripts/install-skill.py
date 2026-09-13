#!/usr/bin/env python3
"""Install this skill as safe, repeatable Claude and/or Codex symlinks."""

import argparse
import os
import sys
import unicodedata
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
    if parent.is_symlink():
        if not parent.exists() or not parent.resolve().is_dir():
            fail(f"destination parent is not a directory: {parent}")
        return
    if not parent.is_dir():
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


def intended_destination(destination):
    """Canonicalize the parent without following a destination link itself."""
    return destination.parent.resolve(strict=False) / destination.name


def is_descendant(path, ancestor):
    path_parts = tuple(unicodedata.normalize("NFD", part).casefold() for part in path.parts)
    ancestor_parts = tuple(
        unicodedata.normalize("NFD", part).casefold() for part in ancestor.parts
    )
    return len(path_parts) > len(ancestor_parts) and path_parts[: len(ancestor_parts)] == ancestor_parts


def reject_source_containment(destinations):
    source = SKILL.resolve()
    lexical_source = Path(os.path.abspath(SKILL))
    for destination in destinations:
        if is_descendant(intended_destination(destination), source) or is_descendant(
            Path(os.path.abspath(destination)), lexical_source
        ):
            fail(f"refusing destination inside skill source: {destination}")


def reject_nested_destinations(destinations):
    identities = [intended_destination(destination) for destination in destinations]
    lexical = [Path(os.path.abspath(destination)) for destination in destinations]
    for index, destination in enumerate(destinations):
        for other_index in range(index):
            other = destinations[other_index]
            if any(
                (
                    is_descendant(left, right)
                    or is_descendant(right, left)
                )
                for left, right in (
                    (lexical[index], lexical[other_index]),
                    (identities[index], identities[other_index]),
                )
            ):
                fail(f"refusing nested destinations: {destination} and {other}")
            # An already-installed destination resolves to the skill source,
            # so compare existing ancestors separately to retain the intended
            # destination boundary through aliases.
            for target, candidate in ((destination, other), (other, destination)):
                if not target.exists():
                    continue
                ancestor = candidate.parent
                while ancestor != ancestor.parent:
                    if ancestor.exists() and os.path.samefile(target, ancestor):
                        fail(f"refusing nested destinations: {destination} and {other}")
                    ancestor = ancestor.parent
    return identities


def install(args):
    if not (SKILL / "SKILL.md").is_file():
        fail(f"skill source has no SKILL.md: {SKILL}")
    if args.target_home == "":
        fail("target-home must not be empty")
    target_home = (
        Path(args.target_home).expanduser() if args.target_home is not None else None
    )
    targets = destinations(args.client, target_home)
    reject_source_containment([destination for _, destination in targets])
    # Check every target before making a directory or link. This keeps `both`
    # atomic with respect to known conflicts.
    states = [
        (
            name,
            destination,
            preflight(destination),
        )
        for name, destination in targets
    ]
    identities = reject_nested_destinations([destination for _, destination, _ in states])
    states = [(*state, identities[index]) for index, state in enumerate(states)]
    completed = set()
    for name, destination, state, physical_destination in states:
        if physical_destination in completed:
            print(f"{name}: shares installed destination {destination}")
            continue
        if state == "present":
            print(f"{name}: already installed at {destination}")
            completed.add(physical_destination)
            continue
        if preflight(destination) == "present":
            print(f"{name}: already installed at {destination}")
            completed.add(physical_destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(SKILL, destination, target_is_directory=True)
        completed.add(physical_destination)
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
