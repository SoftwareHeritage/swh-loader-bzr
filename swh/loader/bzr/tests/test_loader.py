# Copyright (C) 2022-2023  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from datetime import datetime
import os
from pathlib import Path

from breezy.builtins import cmd_uncommit
from breezy.revision import Revision as BzrRevision
from breezy.tests import TestCaseWithTransport
import pytest

from swh.loader.bzr.loader import BazaarLoader, BzrDirectory
from swh.loader.tests import (
    assert_last_visit_matches,
    get_stats,
    prepare_repository_from_archive,
)
from swh.model.from_disk import Content, DentryPerms
from swh.model.hashutil import hash_to_bytes
from swh.model.model import Directory
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
# - does-not-support-tags
#   - Repo is recent but branch does not support tags, needs upgraded

# TODO tests:
# - Root path listed in changes (does that even happen?)
# - Parent is :null (does that even happen?)
# - Case insensitive removal (Is it actually a problem?)
# - Truly corrupted revision?
# - No match from storage (wrong topo sort or broken rev)


def do_uncommit(repo_url):
    """Remove the latest revision from the given bzr repo"""
    uncommit_cmd = cmd_uncommit()
    with open(os.devnull, "w") as f:
        uncommit_cmd.outf = f
        uncommit_cmd.run(repo_url)


@pytest.mark.parametrize("do_clone", [False, True])
def test_nominal(swh_storage, datadir, tmp_path, do_clone):
    archive_path = Path(datadir, "nominal.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    if do_clone:
        # Check that the cloning mechanism works
        loader = BazaarLoader(swh_storage, repo_url, check_revision=1)
    else:
        loader = BazaarLoader(
            swh_storage, repo_url, directory=repo_url, check_revision=1
        )
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
        "directory": 8,
        "origin": 1,
        "origin_visit": 1,
        "release": 3,
        "revision": 6,
        "skipped_content": 0,
        "snapshot": 1,
    }
    # It contains associated bugs, making it a good complete candidate
    example_revision = hash_to_bytes("3c3f39bd71fcfd63a7cfe874dcf206928ea6b414")
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
            "offset_bytes": b"+0100",
        },
        "committer_date": {
            "timestamp": {"seconds": 1643302390, "microseconds": 0},
            "offset_bytes": b"+0100",
        },
        "type": "bzr",
        "directory": hash_to_bytes("afd4a07b7a41f95b43793d319844517bc7dd8655"),
        "synthetic": False,
        "metadata": None,
        "parents": (hash_to_bytes("99327840dec4671728dae17f44727a226d7c03d1"),),
        "id": example_revision,
        "extra_headers": (
            (b"time_offset_seconds", b"3600"),
            (b"bug", b"fixed https://launchpad.net/bugs/1234"),
            (b"bug", b"fixed https://bz.example.com/?show_bug=4321"),
        ),
    }

    # check symlink link -> dir in root directory is correctly archived
    link_entry = [
        entry
        for entry in loader.storage.directory_ls(revision.directory)
        if entry["type"] == "file"
        and entry["perms"] == DentryPerms.symlink
        and entry["name"] == b"link"
    ]
    assert link_entry
    assert loader.storage.content_get_data(link_entry[0]["sha1"]) == b"dir"


def test_needs_upgrade(swh_storage, datadir, tmp_path, mocker):
    """Old bzr repository format should be upgraded to latest format"""
    archive_path = Path(datadir, "needs-upgrade.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "needs-upgrade", tmp_path)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url)
    upgrade_spy = mocker.spy(loader, "run_upgrade")
    res = loader.load()
    upgrade_spy.assert_called()
    assert res == {"status": "uneventful"}  # needs-upgrade is an empty repo


def test_does_not_support_tags(swh_storage, datadir, tmp_path, mocker):
    """Repository format is correct, but the branch itself does not support tags
    and should be upgraded to the latest format"""
    archive_path = Path(datadir, "does-not-support-tags.tgz")
    path = "does-not-support-tags-repo/does-not-support-tags-branch"
    repo_url = prepare_repository_from_archive(
        archive_path,
        path,
        tmp_path,
    )

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url)
    upgrade_spy = mocker.spy(loader, "run_upgrade")
    res = loader.load()
    upgrade_spy.assert_called()
    assert res == {"status": "uneventful"}  # does-not-support-tags is an empty repo


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

    # Empty snapshot does not bother the incremental code
    res = BazaarLoader(swh_storage, repo_url, directory=repo_url).load()
    assert res == {"status": "uneventful"}


def test_renames(swh_storage, datadir, tmp_path):
    archive_path = Path(datadir, "renames.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "renames", tmp_path)

    res = BazaarLoader(
        swh_storage, repo_url, directory=repo_url, check_revision=1
    ).load()
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

    res = BazaarLoader(
        swh_storage, repo_url, directory=repo_url, check_revision=1
    ).load()
    assert res == {"status": "eventful"}

    assert_last_visit_matches(swh_storage, repo_url, status="full", type="bzr")

    snapshot = snapshot_get_latest(swh_storage, repo_url)

    assert sorted(snapshot.branches.keys()) == [
        b"HEAD",
        b"trunk",
    ]

    stats = get_stats(swh_storage)
    assert stats == {
        "content": 3,
        "directory": 10,
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

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
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

    # no KeyError
    directory[b"a/decently"]
    directory[b"a"]
    directory[b"another_node"]


def test_incremental_noop(swh_storage, datadir, tmp_path):
    """Check that nothing happens if we try to load a repo twice in a row"""
    archive_path = Path(datadir, "nominal.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "eventful"}
    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "uneventful"}


def test_incremental_nominal(swh_storage, datadir, tmp_path):
    """Check that an updated repository does update after the second run, but
    is still a noop in the third run."""
    archive_path = Path(datadir, "nominal.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    # remove 2 latest commits
    do_uncommit(repo_url)
    do_uncommit(repo_url)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "eventful"}
    stats = get_stats(swh_storage)
    assert stats == {
        "content": 6,
        "directory": 5,
        "origin": 1,
        "origin_visit": 1,
        "release": 2,
        "revision": 4,
        "skipped_content": 0,
        "snapshot": 1,
    }

    # Load the complete repo now
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "eventful"}

    stats = get_stats(swh_storage)
    expected_stats = {
        "content": 7,
        "directory": 8,
        "origin": 1,
        "origin_visit": 2,
        "release": 3,
        "revision": 6,
        "skipped_content": 0,
        "snapshot": 2,
    }

    assert stats == expected_stats

    # Nothing should change
    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "uneventful"}

    stats = get_stats(swh_storage)
    assert stats == {**expected_stats, "origin_visit": 2 + 1}


def test_incremental_uncommitted_head(swh_storage, datadir, tmp_path):
    """Check that doing an incremental run with the saved head missing does not
    error out but instead loads everything correctly"""
    archive_path = Path(datadir, "nominal.tgz")
    repo_url = prepare_repository_from_archive(archive_path, "nominal", tmp_path)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "eventful"}
    stats = get_stats(swh_storage)
    expected_stats = {
        "content": 7,
        "directory": 8,
        "origin": 1,
        "origin_visit": 1,
        "release": 3,
        "revision": 6,
        "skipped_content": 0,
        "snapshot": 1,
    }

    assert stats == expected_stats

    # Remove the previously saved head
    do_uncommit(repo_url)

    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url, check_revision=1)
    res = loader.load()
    assert res == {"status": "eventful"}

    # Everything is loaded correctly
    stats = get_stats(swh_storage)
    assert stats == {**expected_stats, "origin_visit": 1 + 1, "snapshot": 1 + 1}


@pytest.mark.parametrize("committer", ["John Doe <john.doe@example.org>", "", None])
def test_store_revision_with_empty_or_none_committer(swh_storage, mocker, committer):
    repo_url = "https://example.org/bzr"
    loader = BazaarLoader(swh_storage, repo_url, directory=repo_url)

    mocker.patch.object(
        loader, "store_directories", return_value=Directory(entries=()).id
    )

    author = "John Doe <john.doe@example.org>"
    bzr_rev_id = b"john.doe@example.org-20090420060159-7k8cgljzk05xcm0l"
    bzr_rev = BzrRevision(revision_id=bzr_rev_id, properties={"author": author})
    bzr_rev.committer = committer
    bzr_rev.timestamp = datetime.now().timestamp()
    bzr_rev.timezone = 0
    bzr_rev.message = "test"

    loader.store_revision(bzr_rev)
    loader.flush()

    swh_rev_id = loader._get_revision_id_from_bzr_id(bzr_rev_id)
    swh_rev = swh_storage.revision_get([swh_rev_id])[0]

    if committer is not None:
        assert swh_rev.committer.fullname == committer.encode()
        assert swh_rev.committer_date is not None
    else:
        assert swh_rev.committer is None
        assert swh_rev.committer_date is None


class TestBzrLoader(TestCaseWithTransport):
    @pytest.fixture(autouse=True)
    def storage(self, swh_storage):
        self.swh_storage = swh_storage

    def test_empty_dirs_are_preserved(self):
        working_tree = self.make_branch_and_tree(".")

        dirs_and_files = ["foo/", "foo/foo.py", "bar/", "bar/bar.py", "baz/", "foobar"]
        self.build_tree(dirs_and_files)
        working_tree.add(dirs_and_files)
        working_tree.commit(message="add dirs and files", rev_id=b"rev1")

        os.remove("foo/foo.py")
        os.remove("bar/bar.py")
        working_tree.commit(message="remove files in dirs", rev_id=b"rev2")

        os.remove("foobar")
        self.build_tree(["foobar/"])
        working_tree.add(["foobar/"])
        working_tree.commit(
            message="turn foobar file into an empty directory", rev_id=b"rev3"
        )

        rev_tree = working_tree.branch.repository.revision_tree(b"rev3")

        assert rev_tree.has_filename("foo")
        assert rev_tree.has_filename("bar")
        assert rev_tree.has_filename("baz")
        assert rev_tree.has_filename("foobar")

        repo_url = self.get_url()
        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        assert loader.load() == {"status": "eventful"}

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)
        swh_rev_id = snapshot.branches[b"trunk"].target
        swh_rev = self.swh_storage.revision_get([swh_rev_id])[0]

        swh_root_dir_entries = list(self.swh_storage.directory_ls(swh_rev.directory))

        assert {
            entry["name"] for entry in swh_root_dir_entries if entry["type"] == "dir"
        } == {b"foo", b"bar", b"baz", b"foobar"}

    def test_empty_dirs_removal(self):
        working_tree = self.make_branch_and_tree(".")

        dirs = ["foo/", "foo/foo/", "bar/", "bar/bar/"]
        self.build_tree(dirs)
        working_tree.add(dirs)
        working_tree.commit(message="add dirs and subdirs", rev_id=b"rev1")

        os.rmdir("foo/foo")
        os.rmdir("bar/bar")
        working_tree.commit(message="remove empty subdirs", rev_id=b"rev2")

        repo_url = self.get_url()
        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        assert loader.load() == {"status": "eventful"}

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)
        swh_rev_id = snapshot.branches[b"trunk"].target
        swh_rev = self.swh_storage.revision_get([swh_rev_id])[0]

        swh_root_dir_entries = list(
            self.swh_storage.directory_ls(swh_rev.directory, recursive=True)
        )

        assert {
            entry["name"] for entry in swh_root_dir_entries if entry["type"] == "dir"
        } == {b"foo", b"bar"}
