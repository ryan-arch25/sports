"""Container entrypoint: make DATA_DIR usable, then drop privileges.

Railway mounts a volume owned by root. A container that starts as an
unprivileged user cannot create anything inside that mount, so the first scan
dies with `PermissionError: [Errno 13] Permission denied: '/data'`.

This runs as root just long enough to create DATA_DIR and its subdirectories
and hand them to the application user, then `exec`s the real command as that
user. Only these few lines ever hold privilege, and because it execs rather
than forks, the command still runs as PID 1 and receives signals directly.

It is a no-op when the container is already started as a non-root user
(`docker run --user ...`), in which case the volume has to be writable by that
user to begin with.
"""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

DEFAULT_USER = "cfbedge"
DEFAULT_DATA_DIR = "/app/data"

# What the scan writes to, mirroring the layout `DATA_DIR` sets up in
# cfb_edge.web.app.apply_env_overrides. Creating them here means they are owned
# by the app user from the first boot rather than by whoever wrote them first.
SUBDIRS = ("cache", "runs")


def log(message: str) -> None:
    print(f"[entrypoint] {message}", file=sys.stderr, flush=True)


def lookup_user(name: str) -> pwd.struct_passwd | None:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def own_tree(root: Path, uid: int, gid: int) -> list[str]:
    """Give `root` and everything under it to uid:gid.

    Entries that already have the right owner are left alone, so a restart with
    a large odds cache does not re-chown thousands of files.
    """
    problems: list[str] = []

    def take(target: Path) -> None:
        try:
            info = target.lstat()
        except OSError as exc:
            problems.append(f"{target}: {exc}")
            return
        if info.st_uid == uid and info.st_gid == gid:
            return
        try:
            os.chown(target, uid, gid, follow_symlinks=False)
        except OSError as exc:
            problems.append(f"could not chown {target}: {exc}")

    take(root)
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            take(Path(parent) / name)
    return problems


def prepare(data_dir: str | Path, uid: int, gid: int, subdirs: tuple[str, ...] = SUBDIRS) -> list[str]:
    """Create the data directory tree and hand it to the app user."""
    base = Path(data_dir)
    problems: list[str] = []
    for target in (base, *(base / name for name in subdirs)):
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"could not create {target}: {exc}")
    if base.exists():
        problems.extend(own_tree(base, uid, gid))
    return problems


def drop_privileges(entry: pwd.struct_passwd) -> None:
    """Become `entry`, giving up root for good. Group first: after setuid
    the process can no longer change its groups."""
    try:
        os.initgroups(entry.pw_name, entry.pw_gid)
    except OSError as exc:  # pragma: no cover - needs a real root process
        log(f"warning: could not set supplementary groups: {exc}")
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        log("no command to run; give one after `python -m cfb_edge.entrypoint`")
        return 2

    data_dir = os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR
    username = os.environ.get("APP_USER") or DEFAULT_USER

    if os.geteuid() == 0:
        entry = lookup_user(username)
        if entry is None:
            log(f"no such user {username!r}; running the command as root")
        else:
            for problem in prepare(data_dir, entry.pw_uid, entry.pw_gid):
                log(f"warning: {problem}")
            log(f"prepared {data_dir} for {username} (uid {entry.pw_uid})")
            drop_privileges(entry)
            log(f"dropped privileges to {username}")
    else:
        log(f"already running as uid {os.geteuid()}; leaving {data_dir} as it is")

    try:
        os.execvp(command[0], command)
    except OSError as exc:
        log(f"could not run {command[0]!r}: {exc}")
        return 127
    return 0  # pragma: no cover - execvp does not return on success


if __name__ == "__main__":
    raise SystemExit(main())
