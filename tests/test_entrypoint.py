"""Tests for the container entrypoint that prepares a mounted volume.

The real failure this fixes — a root-owned Railway volume that the app user
cannot write to — needs two uids to reproduce, so these tests cover the pieces:
the directory preparation, the ownership pass, and the order of operations in
main(). The privilege drop itself is exercised by the Dockerfile at run time.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import cfb_edge.entrypoint as entrypoint
from cfb_edge.entrypoint import (
    SUBDIRS,
    lookup_user,
    main,
    own_tree,
    prepare,
)

ME = SimpleNamespace(pw_name="cfbedge", pw_uid=os.getuid(), pw_gid=os.getgid())


@pytest.fixture
def fake_exec(monkeypatch):
    """Capture the exec instead of replacing the test process."""
    captured: dict = {}
    monkeypatch.setattr(entrypoint.os, "execvp", lambda f, a: captured.update(file=f, args=a))
    return captured


class TestPrepare:
    def test_creates_the_data_directory_and_its_subdirectories(self, tmp_path):
        data = tmp_path / "data"
        assert prepare(data, os.getuid(), os.getgid()) == []
        assert data.is_dir()
        for name in SUBDIRS:
            assert (data / name).is_dir()

    def test_creates_missing_parents(self, tmp_path):
        data = tmp_path / "deep" / "nested" / "data"
        assert prepare(data, os.getuid(), os.getgid()) == []
        assert (data / "cache").is_dir()

    def test_is_idempotent(self, tmp_path):
        data = tmp_path / "data"
        prepare(data, os.getuid(), os.getgid())
        (data / "cache" / "odds.json").write_text("{}", encoding="utf-8")
        assert prepare(data, os.getuid(), os.getgid()) == []
        assert (data / "cache" / "odds.json").read_text(encoding="utf-8") == "{}"

    def test_leaves_existing_files_in_place(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "cfb_edge.sqlite").write_bytes(b"not really a database")
        prepare(data, os.getuid(), os.getgid())
        assert (data / "cfb_edge.sqlite").read_bytes() == b"not really a database"

    def test_reports_a_directory_it_cannot_create(self, tmp_path, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", refuse)
        problems = prepare(tmp_path / "data", os.getuid(), os.getgid())
        assert problems and all("could not create" in p for p in problems)

    def test_a_custom_subdirectory_list(self, tmp_path):
        prepare(tmp_path / "data", os.getuid(), os.getgid(), subdirs=("only",))
        assert (tmp_path / "data" / "only").is_dir()
        assert not (tmp_path / "data" / "cache").exists()


class TestOwnTree:
    def test_skips_entries_that_already_have_the_right_owner(self, tmp_path, monkeypatch):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "odds.json").write_text("{}", encoding="utf-8")
        calls = []
        monkeypatch.setattr(
            entrypoint.os, "chown", lambda *a, **k: calls.append(a)
        )
        assert own_tree(tmp_path, os.getuid(), os.getgid()) == []
        assert calls == []  # nothing needed changing, so nothing was touched

    def test_chowns_everything_that_does_not_match(self, tmp_path, monkeypatch):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "odds.json").write_text("{}", encoding="utf-8")
        (tmp_path / "runs").mkdir()
        chowned = []
        monkeypatch.setattr(
            entrypoint.os, "chown",
            lambda path, uid, gid, follow_symlinks=True: chowned.append(Path(path).name),
        )
        assert own_tree(tmp_path, 10001, 10001) == []
        assert sorted(chowned) == sorted([tmp_path.name, "cache", "odds.json", "runs"])

    def test_reports_a_chown_that_fails(self, tmp_path, monkeypatch):
        def refuse(*args, **kwargs):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(entrypoint.os, "chown", refuse)
        problems = own_tree(tmp_path, 10001, 10001)
        assert problems and "could not chown" in problems[0]

    def test_a_broken_symlink_is_reported_not_fatal(self, tmp_path, monkeypatch):
        (tmp_path / "dangling").symlink_to(tmp_path / "missing")
        monkeypatch.setattr(entrypoint.os, "chown", lambda *a, **k: None)
        assert own_tree(tmp_path, 10001, 10001) == []


class TestLookupUser:
    def test_finds_a_real_user(self):
        assert lookup_user("root") is not None

    def test_returns_none_for_an_unknown_user(self):
        assert lookup_user("definitely-not-a-user-here") is None


class TestMain:
    def test_refuses_to_run_with_no_command(self, capsys):
        assert main([]) == 2
        assert "no command" in capsys.readouterr().err

    def test_as_a_normal_user_it_only_execs(self, tmp_path, monkeypatch, fake_exec, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 10001)
        monkeypatch.setattr(
            entrypoint, "prepare", lambda *a, **k: pytest.fail("must not touch the volume")
        )
        assert main(["uvicorn", "app:app"]) == 0
        assert fake_exec["args"] == ["uvicorn", "app:app"]
        assert "already running as uid 10001" in capsys.readouterr().err

    def test_as_root_it_prepares_then_drops_then_execs(self, tmp_path, monkeypatch, fake_exec):
        order = []
        data = tmp_path / "vol"
        monkeypatch.setenv("DATA_DIR", str(data))
        monkeypatch.setenv("APP_USER", "cfbedge")
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
        monkeypatch.setattr(entrypoint, "lookup_user", lambda name: ME)
        real_prepare = entrypoint.prepare

        def traced_prepare(*args, **kwargs):
            order.append("prepare")
            return real_prepare(*args, **kwargs)

        monkeypatch.setattr(entrypoint, "prepare", traced_prepare)
        monkeypatch.setattr(entrypoint, "drop_privileges", lambda e: order.append("drop"))
        monkeypatch.setattr(
            entrypoint.os, "execvp",
            lambda f, a: (order.append("exec"), fake_exec.update(file=f, args=a)),
        )

        assert main(["uvicorn", "app:app"]) == 0
        assert order == ["prepare", "drop", "exec"]
        assert data.is_dir() and (data / "cache").is_dir()

    def test_as_root_with_no_such_user_it_stays_root(self, tmp_path, monkeypatch, fake_exec, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("APP_USER", "ghost")
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
        monkeypatch.setattr(entrypoint, "lookup_user", lambda name: None)
        monkeypatch.setattr(
            entrypoint, "drop_privileges", lambda e: pytest.fail("nobody to drop to")
        )
        assert main(["uvicorn"]) == 0
        assert "no such user 'ghost'" in capsys.readouterr().err

    def test_problems_are_warned_about_but_do_not_stop_the_boot(
        self, tmp_path, monkeypatch, fake_exec, capsys
    ):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
        monkeypatch.setattr(entrypoint, "lookup_user", lambda name: ME)
        monkeypatch.setattr(entrypoint, "drop_privileges", lambda e: None)
        monkeypatch.setattr(entrypoint, "prepare", lambda *a, **k: ["could not chown /vol: nope"])
        assert main(["uvicorn"]) == 0
        assert "warning: could not chown" in capsys.readouterr().err
        assert fake_exec["args"] == ["uvicorn"]

    def test_the_default_data_dir_is_used_when_unset(self, monkeypatch, fake_exec):
        seen = {}
        monkeypatch.delenv("DATA_DIR", raising=False)
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
        monkeypatch.setattr(entrypoint, "lookup_user", lambda name: ME)
        monkeypatch.setattr(entrypoint, "drop_privileges", lambda e: None)
        monkeypatch.setattr(
            entrypoint, "prepare", lambda data_dir, *a, **k: seen.update(dir=str(data_dir)) or []
        )
        main(["uvicorn"])
        assert seen["dir"] == entrypoint.DEFAULT_DATA_DIR

    def test_a_command_that_does_not_exist_is_reported(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 10001)

        def missing(file, args):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(entrypoint.os, "execvp", missing)
        assert main(["definitely-not-installed"]) == 127
        assert "could not run" in capsys.readouterr().err
