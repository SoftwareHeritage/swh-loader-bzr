# Copyright (C) 2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information
from pathlib import Path

import pytest

import swh.loader.bzr.loader as loader_mod
from swh.loader.bzr.loader import BazaarLoader, BzrDirectory
from swh.loader.tests import (
    assert_last_visit_matches,
    get_stats,
    prepare_repository_from_archive,
)
from swh.model.from_disk import Content
from swh.model.hashutil import hash_to_bytes
from swh.storage.algos.snapshot import snapshot_get_latest

# Generated repositories:
# - needs-upgrade:
#   - Repository needs upgrade
# - empty:
#   - Empty repo
# - renames:
#   - File rename
#   - Directory renames
#   - Directory renames *and* file rename conflicting
# - no-branch:
#   - No branch
# - metadata-and-type-changes:
#   - Directory removed
#   - Kind changed (file to symlink, directory to file, etc.)
#   - not changed_content and not renamed and not kind_changed (so, exec file?)
#   - Executable file
#   - Empty commit (bzr commit --unchanged)
# - ghosts
#   - Ghost revisions
# - broken-tags
#   - Tags corruption

# TODO tests:
# - Root path listed in changes (does that even happen?)
# - Parent is :null (does that even happen?)
# - Case insensitive removal (Is it actually a problem?)
# - Truly corrupted revision?
# - No match from storage (wrong topo sort or broken rev)


@pytest.mark.parametrize("do_clone", [False, True])
def test_nominal(swh_storage, datadir, tmp_path, do_clone):
    archive_path = Path(datadir, "nominal.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    if do_clone:
        # Check that the cloning mechanism works
        loader = BazaarLoader(swh_storage, repo_url)
    else:
        loader = BazaarLoader(swh_storage, repo_url, directory=repo_url)
    res = loader.load()
    assert res == {"status": "eventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    expected_branches = [
        b"HEAD",
        b"tags/0.1",
        b"tags/latest",
        b"tags/other-tag",
        b"trunk",
    ]
    assert sorted(snapshot.branches.keys()) == expected_branches

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 7,
        "directory": 7,
        "origin": 1,
        "origin_visit": 1,
        "release": 3,
        "revision": 6,
        "skipped_content": 0,
        "snapshot": 1,
    }
    # It contains associated bugs, making it a good complete candidate
    example_revision = hash_to_bytes("18bb5b2c866c10c58a191afcd0b450a8727f1c62")
    revision = loader.storage.revision_get([example_revision])[0]
    assert revision.to_dict() == {
        "message": b"fixing bugs",
        "author": {
            "fullname": b"Rapha\xc3\xabl Gom\xc3\xa8s <alphare@alphare-carbon.lan>",
            "name": b"Rapha\xc3\xabl Gom\xc3\xa8s",
            "email": b"alphare@alphare-carbon.lan",
        },
        "committer": {
            "fullname": b"Rapha\xc3\xabl Gom\xc3\xa8s <alphare@alphare-carbon.lan>",
            "name": b"Rapha\xc3\xabl Gom\xc3\xa8s",
            "email": b"alphare@alphare-carbon.lan",
        },
        "date": {
            "timestamp": {"seconds": 1643302390, "microseconds": 0},
            "offset": 60,
            "negative_utc": False,
            "offset_bytes": b"+0100",
        },
        "committer_date": {
            "timestamp": {"seconds": 1643302390, "microseconds": 0},
            "offset": 60,
            "negative_utc": False,
            "offset_bytes": b"+0100",
        },
        "type": "bzr",
        "directory": b"s0\xf3pe\xa3\x12\x05{\xc7\xbc\x86\xa6\x14.\xc1b\x1c\xeb\x05",
        "synthetic": False,
        "metadata": None,
        "parents": (b"*V\xf5\n\xf0?\x1d{kE4\xda(\xb1\x08R\x83\x87-\xb6",),
        "id": example_revision,
        "extra_headers": (
            (b"time_offset_seconds", b"3600"),
            (b"bug", b"fixed https://launchpad.net/bugs/1234"),
            (b"bug", b"fixed https://bz.example.com/?show_bug=4321"),
        ),
    }


def test_needs_upgrade(swh_storage, datadir, tmp_path, mocker):
    archive_path = Path(datadir, "needs-upgrade.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "needs-upgrade", tmp_path)

    init_spy = mocker.spy(loader_mod.RepositoryNeedsUpgrade, "__init__")
    init_spy.assert_not_called()
    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    init_spy.assert_called()
    assert res == {"status": "failed"}


def test_no_branch(swh_storage, datadir, tmp_path):
    """This should only happen with a broken clone, so the expected result is failure"""
    archive_path = Path(datadir, "no-branch.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "no-branch", tmp_path)

    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "failed"}


def test_empty(swh_storage, datadir, tmp_path):
    """An empty repository is fine, it's just got no information"""
    archive_path = Path(datadir, "empty.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "empty", tmp_path)

    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "uneventful"}


def test_renames(swh_storage, datadir, tmp_path):
    archive_path = Path(datadir, "renames.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "renames", tmp_path)

    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "eventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    assert sorted(snapshot.branches.keys()) == [
        b"HEAD",
        b"trunk",
    ]

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 1,
        "directory": 5,
        "origin": 1,
        "origin_visit": 1,
        "release": 0,
        "revision": 2,
        "skipped_content": 0,
        "snapshot": 1,
    }


def test_broken_tags(swh_storage, datadir, tmp_path):
    """A tag pointing to a the null revision should not break anything"""
    archive_path = Path(datadir, "broken-tags.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "broken-tags", tmp_path)

    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "uneventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    assert sorted(snapshot.branches.keys()) == [
        b"tags/null-tag",  # broken tag does appear, but didn't cause any issues
    ]

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 0,
        "directory": 0,
        "origin": 1,
        "origin_visit": 1,
        "release": 0,  # Does not count as a valid release
        "revision": 0,
        "skipped_content": 0,
        "snapshot": 1,
    }


def test_metadata_and_type_changes(swh_storage, datadir, tmp_path):
    archive_path = Path(datadir, "metadata-and-type-changes.tgz")
    repo_url = prepare_repository_from_archive(
        archive_path, "metadata-and-type-changes", tmp_path
    )

    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "eventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    assert sorted(snapshot.branches.keys()) == [
        b"HEAD",
        b"trunk",
    ]

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 1,
        "directory": 9,
        "origin": 1,
        "origin_visit": 1,
        "release": 0,
        "revision": 7,
        "skipped_content": 0,
        "snapshot": 1,
    }


def test_ghosts(swh_storage, datadir, tmp_path):
    archive_path = Path(datadir, "ghosts.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "ghosts", tmp_path)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url)
    assert loader._ghosts == set()
    res = loader.load()
    assert loader._ghosts == set((b"iamaghostboo",))
    assert res == {"status": "eventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    assert sorted(snapshot.branches.keys()) == [
        b"HEAD",
        b"tags/brokentag",  # tag pointing to a ghost revision is tracked
        b"trunk",
    ]

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 0,  # No contents
        "directory": 1,  # Root directory always counts
        "origin": 1,
        "origin_visit": 1,
        "release": 0,  # Ghost tag is ignored, stored as dangling
        "revision": 1,  # Only one revision, the ghost is ignored
        "skipped_content": 0,
        "snapshot": 1,
    }


def test_bzr_directory():
    directory = BzrDirectory()
    directory[b"a/decently/enough/nested/path"] = Content(b"whatever")
    directory[b"a/decently/other_node"] = Content(b"whatever else")
    directory[b"another_node"] = Content(b"contents")

    assert directory[b"a/decently/enough/nested/path"] == Content(b"whatever")
    assert directory[b"a/decently/other_node"] == Content(b"whatever else")
    assert directory[b"another_node"] == Content(b"contents")

    del directory[b"a/decently/enough/nested/path"]
    assert directory.get(b"a/decently/enough/nested/path") is None
    assert directory.get(b"a/decently/enough/nested/") is None
    assert directory.get(b"a/decently/enough") is None

    # no KeyError
    directory[b"a/decently"]
    directory[b"a"]
    directory[b"another_node"]
