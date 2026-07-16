#!/usr/bin/env python3
"""Synchronize this plugin's project payload into a target project.

Files managed by the plugin are authoritative and replace different files at
the same destination path. Unrelated files and directories remain untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_RELATIVE_PATH = Path("scripts/install-project.py")


@dataclass(frozen=True)
class PayloadRoot:
    source: Path
    destination: Path


@dataclass
class MergePlan:
    copy: list[tuple[Path, Path]] = field(default_factory=list)
    overwrite: list[tuple[Path, Path]] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    default_target = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize bundled .claude skills and scripts into a project, "
            "overwriting plugin-managed files."
        )
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path(default_target),
        help="project directory (default: CLAUDE_PROJECT_DIR, then current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change without copying files",
    )
    return parser.parse_args(argv)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def same_file_content(source: Path, destination: Path) -> bool:
    try:
        if source.stat().st_size != destination.stat().st_size:
            return False
        return sha256(source) == sha256(destination)
    except OSError:
        return False


def is_excluded(source: Path) -> bool:
    relative = source.relative_to(PLUGIN_ROOT)
    if relative == INSTALLER_RELATIVE_PATH:
        return True
    return any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts)


def iter_payload_files(root: PayloadRoot) -> Iterable[tuple[Path, Path]]:
    if not root.source.is_dir():
        raise RuntimeError(f"bundled payload directory is missing: {root.source}")

    for source in sorted(root.source.rglob("*")):
        if source.is_symlink():
            raise RuntimeError(f"symlinks are not allowed in the payload: {source}")
        if not source.is_file() or is_excluded(source):
            continue
        relative = source.relative_to(root.source)
        yield source, root.destination / relative


def ensure_target_is_safe(target: Path) -> Path:
    target = target.expanduser()
    if not target.exists():
        raise RuntimeError(f"target directory does not exist: {target}")
    if not target.is_dir():
        raise RuntimeError(f"target is not a directory: {target}")
    if target.is_symlink():
        raise RuntimeError(f"target directory must not be a symlink: {target}")
    return target.resolve()


def destination_is_within_target(destination: Path, target: Path) -> bool:
    try:
        destination.resolve(strict=False).relative_to(target)
        return True
    except ValueError:
        return False


def ensure_destination_parents_are_safe(destination: Path, target: Path) -> None:
    current = destination.parent
    while current != target:
        if current.is_symlink():
            raise RuntimeError(f"destination parent must not be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise RuntimeError(f"destination parent is not a directory: {current}")
        current = current.parent


def build_plan(target: Path) -> MergePlan:
    roots = (
        PayloadRoot(PLUGIN_ROOT / ".claude" / "skills", target / ".claude" / "skills"),
        PayloadRoot(PLUGIN_ROOT / "scripts", target / "scripts"),
    )
    plan = MergePlan()

    for root in roots:
        for source, destination in iter_payload_files(root):
            if not destination_is_within_target(destination, target):
                raise RuntimeError(f"destination escapes target directory: {destination}")
            ensure_destination_parents_are_safe(destination, target)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink():
                    raise RuntimeError(f"destination file must not be a symlink: {destination}")
                if not destination.is_file():
                    raise RuntimeError(f"destination is not a regular file: {destination}")
                if same_file_content(source, destination):
                    plan.unchanged.append(destination)
                else:
                    plan.overwrite.append((source, destination))
            else:
                plan.copy.append((source, destination))

    return plan


def directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def open_destination_parent(target_fd: int, parent_parts: Sequence[str]) -> int:
    """Open/create a destination parent using directory FDs without following symlinks."""
    current_fd = os.dup(target_fd)
    try:
        for part in parent_parts:
            try:
                next_fd = os.open(part, directory_open_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    # Another process created something at this path. The no-follow
                    # open below decides whether it is a safe directory.
                    pass
                next_fd = os.open(part, directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def install_files(plan: MergePlan, target: Path) -> None:
    """Apply a plan relative to an anchored target directory descriptor.

    New files use O_EXCL. Replacements are written to a sibling temporary file
    and atomically renamed over the old path. Every parent is opened with
    O_NOFOLLOW, keeping writes inside the target even during symlink races.
    """
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "fchmod")
        or not all(
            function in os.supports_dir_fd
            for function in (os.open, os.mkdir, os.unlink, os.rename)
        )
    ):
        raise RuntimeError("safe installation requires POSIX directory-fd support")

    target_fd = os.open(target, directory_open_flags())
    try:
        for source, destination in (*plan.copy, *plan.overwrite):
            relative = destination.relative_to(target)
            parent_fd = open_destination_parent(target_fd, relative.parent.parts)
            filename = relative.name
            source_mode = stat.S_IMODE(source.stat().st_mode)
            replacing = (source, destination) in plan.overwrite
            created_name = filename
            if replacing:
                token = secrets.token_hex(6)
                created_name = f".{filename}.docker-claude.{os.getpid()}.{token}.tmp"
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            file_flags |= getattr(os, "O_CLOEXEC", 0)
            created = False
            try:
                with source.open("rb") as source_handle:
                    destination_fd = os.open(created_name, file_flags, source_mode, dir_fd=parent_fd)
                    created = True
                    with os.fdopen(destination_fd, "wb") as destination_handle:
                        shutil.copyfileobj(source_handle, destination_handle)
                        destination_handle.flush()
                        os.fchmod(destination_handle.fileno(), source_mode)
                if replacing:
                    os.rename(
                        created_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    created = False
            except FileExistsError as error:
                raise RuntimeError(
                    f"temporary or destination path appeared during install: {destination}"
                ) from error
            except BaseException:
                if created:
                    try:
                        os.unlink(created_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                raise
            finally:
                os.close(parent_fd)
    finally:
        os.close(target_fd)


def relative_display(path: Path, target: Path) -> str:
    try:
        return str(path.relative_to(target))
    except ValueError:
        return str(path)


def print_plan(plan: MergePlan, target: Path, dry_run: bool) -> None:
    copy_action = "would copy" if dry_run else "copied"
    overwrite_action = "would overwrite" if dry_run else "overwritten"
    print(f"target: {target}")
    for _, destination in plan.copy:
        print(f"  {copy_action:15s} {relative_display(destination, target)}")
    for _, destination in plan.overwrite:
        print(f"  {overwrite_action:15s} {relative_display(destination, target)}")
    for destination in plan.unchanged:
        print(f"  {'unchanged':15s} {relative_display(destination, target)}")

    print()
    print(
        "summary: "
        f"{len(plan.copy)} {'would be copied' if dry_run else 'copied'}, "
        f"{len(plan.overwrite)} {'would be overwritten' if dry_run else 'overwritten'}, "
        f"{len(plan.unchanged)} unchanged"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        target = ensure_target_is_safe(args.target)
        plan = build_plan(target)
        if not args.dry_run:
            install_files(plan, target)
        print_plan(plan, target, args.dry_run)
    except (OSError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
