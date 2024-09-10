# Copyright (C) 2022-2023  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from datetime import datetime
import os
import shutil
import stat
from subprocess import run
import threading

from breezy.bzr.bzrdir import BzrDir
from breezy.commands import builtin_command_names
from breezy.revision import Revision as BzrRevision
from breezy.tests import TestCaseWithTransport
from dulwich.repo import Repo
from dulwich.server import DictBackend, TCPGitServer
import pytest

from swh.loader.bzr.loader import BazaarLoader, BzrDirectory
from swh.loader.tests import assert_last_visit_matches, get_stats
from swh.model.from_disk import Content, DentryPerms
from swh.model.hashutil import hash_to_bytes
from swh.model.model import (
    Directory,
    Person,
    Revision,
    RevisionType,
    Timestamp,
    TimestampWithTimezone,
)
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


# workaround pytest 8.2.0 regression
# https://github.com/pytest-dev/pytest/issues/12263
setattr(TestCaseWithTransport, "runTest", lambda self: None)


class TestBzrLoader(TestCaseWithTransport):
    @pytest.fixture(autouse=True)
    def fixtures(self, swh_storage, mocker, tmp_path):
        self.swh_storage = swh_storage
        self.mocker = mocker
        self.tmp_path = tmp_path

    @classmethod
    def setUpClass(cls):
        # init breezy commands
        builtin_command_names()

    @staticmethod
    def touch(filename):
        with open(filename, "wb") as my_file:
            my_file.write(b"")

    @staticmethod
    def add_file_with_content(path: str, content: bytes):
        with open(path, "wb") as file:
            file.write(content)

    @staticmethod
    def append_to_file(path: str, content: bytes):
        with open(path, "ab") as file:
            file.write(content)

    def build_nominal_repo(self):
        self.run_bzr("init nominal")
        self.add_file_with_content("nominal/a.txt", b"a\n")
        os.mkdir("nominal/dir")
        self.add_file_with_content("nominal/dir/b.txt", b"contents\\nhere\n")
        os.mkdir("nominal/empty-dir")
        self.add_file_with_content("nominal/dir/c", b"c\n")
        self.add_file_with_content("nominal/d", b"d\n")
        self.run_bzr("add", working_dir="nominal")
        self.run_bzr(
            [
                "commit",
                "-m",
                "Initial commit",
                "--commit-time",
                "2019-10-10 08:00:00 +0000",
            ],
            working_dir="nominal",
        )

        self.run_bzr("branch nominal nominal-branch")

        self.append_to_file("nominal-branch/a.txt", b"other text\n")
        self.run_bzr("add", working_dir="nominal-branch")
        self.run_bzr(
            [
                "commit",
                "-m",
                "Modified a \nThis change happened in another branch",
                "--commit-time",
                "2019-10-12 08:00:00 +0000",
            ],
            working_dir="nominal-branch",
        )

        self.run_bzr("merge ../nominal-branch", working_dir="nominal")
        self.run_bzr(
            ["commit", "-m", "merge", "--commit-time", "2019-10-14 08:00:00 +0000"],
            working_dir="nominal",
        )

        os.symlink("dir", "nominal/link")
        self.run_bzr("add", working_dir="nominal")
        self.run_bzr(
            [
                "commit",
                "-m",
                "Add symlink",
                "--commit-time",
                "2019-10-16 08:00:00 +0000",
            ],
            working_dir="nominal",
        )

        os.remove("nominal/d")
        self.run_bzr(
            ["commit", "-m", "deleted d", "--commit-time", "2019-10-18 08:00:00 +0000"],
            working_dir="nominal",
        )

        self.run_bzr("tag -r 2 0.1", working_dir="nominal")
        self.run_bzr("tag -r 2 other-tag", working_dir="nominal")
        self.run_bzr("tag -r 4 latest", working_dir="nominal")
        self.append_to_file("nominal/dir/b.txt", b"fix-bug\n")
        self.run_bzr(
            ["config", 'bugtracker_bz_url="https://bz.example.com/?show_bug={id}"'],
            working_dir="nominal",
        )
        self.run_bzr(
            [
                "commit",
                "-m",
                "fixing bugs",
                "--fixes",
                "lp:1234",
                "--fixes",
                "bz:4321",
                "--commit-time",
                "2019-10-20 08:00:00 +0000",
            ],
            working_dir="nominal",
        )

        return self.get_url() + "/nominal"

    def nominal_test(self, do_clone):
        repo_url = self.build_nominal_repo()
        if do_clone:
            # Check that the cloning mechanism works
            loader = BazaarLoader(self.swh_storage, repo_url, check_revision=1)
        else:
            loader = BazaarLoader(
                self.swh_storage, repo_url, directory=repo_url, check_revision=1
            )
        res = loader.load()
        assert res == {"status": "eventful"}

        assert_last_visit_matches(self.swh_storage, repo_url, status="full", type="bzr")

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)

        expected_branches = [
            b"HEAD",
            b"tags/0.1",
            b"tags/latest",
            b"tags/other-tag",
            b"trunk",
        ]
        assert sorted(snapshot.branches.keys()) == expected_branches

        stats = get_stats(self.swh_storage)
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
        example_revision = snapshot.branches[b"trunk"].target
        revision = loader.storage.revision_get([example_revision])[0]

        assert revision == Revision(
            message=b"fixing bugs",
            author=Person(
                fullname=b"jrandom@example.com", name=b"jrandom@example.com", email=None
            ),
            committer=Person(
                fullname=b"jrandom@example.com", name=b"jrandom@example.com", email=None
            ),
            date=TimestampWithTimezone(
                timestamp=Timestamp(seconds=1571558400, microseconds=0),
                offset_bytes=b"+0000",
            ),
            committer_date=TimestampWithTimezone(
                timestamp=Timestamp(seconds=1571558400, microseconds=0),
                offset_bytes=b"+0000",
            ),
            type=RevisionType.BAZAAR,
            directory=hash_to_bytes("afd4a07b7a41f95b43793d319844517bc7dd8655"),
            synthetic=False,
            metadata=None,
            parents=(hash_to_bytes("d84d6beccae0529da04caeacf0eacff93ca303b9"),),
            id=hash_to_bytes("c441b06f3b248d85cd1ee0ff3f36d913887374e6"),
            extra_headers=(
                (b"time_offset_seconds", b"0"),
                (b"bug", b"fixed https://launchpad.net/bugs/1234"),
                (b"bug", b'fixed "https://bz.example.com/?show_bug=4321"'),
            ),
            raw_manifest=None,
        )

        assert (
            revision.author
            == revision.committer
            == Person(
                fullname=b"jrandom@example.com", name=b"jrandom@example.com", email=None
            )
        )
        assert revision.directory == hash_to_bytes(
            "afd4a07b7a41f95b43793d319844517bc7dd8655"
        )
        assert revision.message == b"fixing bugs"
        assert revision.type == RevisionType.BAZAAR
        assert (
            b"bug",
            b"fixed https://launchpad.net/bugs/1234",
        ) in revision.extra_headers
        assert (
            b"bug",
            b'fixed "https://bz.example.com/?show_bug=4321"',
        ) in revision.extra_headers

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

    def test_nominal_with_clone(self):
        self.disable_directory_isolation()
        self.nominal_test(do_clone=True)

    def test_nominal_without_clone(self):
        self.nominal_test(do_clone=False)

    def test_metadata_and_type_changes(self):
        self.run_bzr("init")

        self.touch("a")
        os.symlink("a", "b")
        os.mkdir("dir")
        self.touch("dir/c")
        self.touch("dir/d")
        self.touch("dir/e")
        os.mkdir("dir/dir2")
        self.touch("dir/dir2/f")
        self.touch("dir/dir2/g")
        self.touch("dir/dir2/h")
        self.run_bzr("add")
        self.run_bzr("commit -minitial")

        os.remove("a")
        os.remove("b")
        self.touch("b")
        os.mkdir("a")
        self.touch("a/file")
        os.rename("a/file", "a/file1")
        os.rename("dir", "dir-other")
        os.symlink("dir-other", "dir")
        self.run_bzr("add")
        self.run_bzr("add a/file1")
        self.run_bzr("commit -mchanges")

        shutil.rmtree("dir-other")
        self.run_bzr("commit -mremove-dir")

        os.chmod("b", os.stat("b").st_mode | stat.S_IEXEC)
        self.run_bzr("commit -mexecutable")
        self.run_bzr("commit -mempty --unchanged")

        shutil.rmtree("a")
        self.touch("a")
        self.run_bzr("add a")
        self.run_bzr("commit -mdir-to-file")

        os.remove("dir")
        os.mkdir("dir")

        self.run_bzr("commit -msymlink-to-dir")

        repo_url = self.get_url()
        res = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        ).load()
        assert res == {"status": "eventful"}

        assert_last_visit_matches(self.swh_storage, repo_url, status="full", type="bzr")

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)

        assert sorted(snapshot.branches.keys()) == [
            b"HEAD",
            b"trunk",
        ]

        stats = get_stats(self.swh_storage)
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

    def test_ghosts(self):
        # Creates a repository with a revision based on a ghost revision, as well
        # as a tag pointing to said ghost.
        tree = BzrDir.create_standalone_workingtree("ghosts")
        tree.add_pending_merge(b"iamaghostboo")
        tree.commit(message="some commit")
        tree.branch.tags.set_tag("brokentag", b"iamaghostboo")

        repo_url = self.get_url() + "/ghosts"

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        assert loader._ghosts == set()
        res = loader.load()
        assert loader._ghosts == set((b"iamaghostboo",))
        assert res == {"status": "eventful"}

        assert_last_visit_matches(self.swh_storage, repo_url, status="full", type="bzr")

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)

        assert sorted(snapshot.branches.keys()) == [
            b"HEAD",
            b"tags/brokentag",  # tag pointing to a ghost revision is tracked
            b"trunk",
        ]

        stats = get_stats(self.swh_storage)
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

    def test_needs_upgrade(self):
        """Old bzr repository format should be upgraded to latest format"""

        self.run_bzr("init --rich-root")

        repo_url = self.get_url()
        loader = BazaarLoader(self.swh_storage, repo_url, directory=repo_url)
        upgrade_spy = self.mocker.spy(loader, "run_upgrade")
        res = loader.load()
        upgrade_spy.assert_called()
        assert res == {"status": "uneventful"}  # needs-upgrade is an empty repo

    def test_does_not_support_tags(self):
        """Repository format is correct, but the branch itself does not support tags
        and should be upgraded to the latest format"""
        self.run_bzr("init-shared-repo does-not-support-tags-repo")
        self.run_bzr(
            "init --knit does-not-support-tags-branch",
            working_dir="does-not-support-tags-repo",
        )

        repo_url = (
            self.get_url() + "/does-not-support-tags-repo/does-not-support-tags-branch"
        )
        loader = BazaarLoader(self.swh_storage, repo_url, directory=repo_url)
        upgrade_spy = self.mocker.spy(loader, "run_upgrade")
        res = loader.load()
        upgrade_spy.assert_called()
        assert res == {"status": "uneventful"}  # does-not-support-tags is an empty repo

    def test_empty(self):
        """An empty repository is fine, it's just got no information"""
        self.run_bzr("init")

        repo_url = self.get_url()
        res = BazaarLoader(self.swh_storage, repo_url, directory=repo_url).load()
        assert res == {"status": "uneventful"}

        # Empty snapshot does not bother the incremental code
        res = BazaarLoader(self.swh_storage, repo_url, directory=repo_url).load()
        assert res == {"status": "uneventful"}

    def test_renames(self):
        self.run_bzr("init")

        os.mkdir("dir1")
        os.mkdir("dir2")
        self.touch("dir1/file1")
        self.touch("dir1/file2")
        self.touch("dir2/file3")
        self.touch("dir2/file4")
        self.run_bzr("add")
        self.run_bzr("commit -mInitial")

        self.run_bzr("mv dir1 dir1-renamed")
        self.run_bzr("mv dir2 dir2-renamed")
        self.run_bzr("mv dir1-renamed/file1 dir1-renamed/file1-renamed")
        self.run_bzr("commit -mrenames")

        repo_url = self.get_url()
        res = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        ).load()
        assert res == {"status": "eventful"}

        assert_last_visit_matches(self.swh_storage, repo_url, status="full", type="bzr")

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)

        assert sorted(snapshot.branches.keys()) == [
            b"HEAD",
            b"trunk",
        ]

        stats = get_stats(self.swh_storage)
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

    def test_broken_tags(self):
        """A tag pointing to a the null revision should not break anything"""
        self.run_bzr("init")
        self.run_bzr("tag null-tag")

        repo_url = self.get_url()
        res = BazaarLoader(self.swh_storage, repo_url, directory=repo_url).load()
        assert res == {"status": "uneventful"}

        assert_last_visit_matches(self.swh_storage, repo_url, status="full", type="bzr")

        snapshot = snapshot_get_latest(self.swh_storage, repo_url)

        assert sorted(snapshot.branches.keys()) == [
            b"tags/null-tag",  # broken tag does appear, but didn't cause any issues
        ]

        stats = get_stats(self.swh_storage)
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

    def test_incremental_noop(self):
        """Check that nothing happens if we try to load a repo twice in a row"""
        repo_url = self.build_nominal_repo()

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "eventful"}

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "uneventful"}

    def test_incremental_nominal(self):
        """Check that an updated repository does update after the second run, but
        is still a noop in the third run."""
        repo_url = self.build_nominal_repo()

        # remove 2 latest commits
        self.run_bzr("uncommit --force", working_dir="nominal")
        self.run_bzr("uncommit --force", working_dir="nominal")

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "eventful"}
        stats = get_stats(self.swh_storage)
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
        shutil.rmtree("nominal")
        shutil.rmtree("nominal-branch")

        repo_url = self.build_nominal_repo()

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "eventful"}

        stats = get_stats(self.swh_storage)
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
        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "uneventful"}

        stats = get_stats(self.swh_storage)
        assert stats == {**expected_stats, "origin_visit": 3}

    def test_incremental_uncommitted_head(self):
        """Check that doing an incremental run with the saved head missing does not
        error out but instead loads everything correctly"""
        repo_url = self.build_nominal_repo()

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "eventful"}
        stats = get_stats(self.swh_storage)
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
        self.run_bzr("uncommit --force", working_dir="nominal")

        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        res = loader.load()
        assert res == {"status": "eventful"}

        # Everything is loaded correctly
        stats = get_stats(self.swh_storage)
        assert stats == {**expected_stats, "origin_visit": 2, "snapshot": 2}

    def test_no_branch(self):
        """This should only happen with a broken clone, so the expected result is failure"""

        self.run_bzr("init")
        self.run_bzr("remove-branch --force")

        repo_url = self.get_url()
        res = BazaarLoader(self.swh_storage, repo_url, directory=repo_url).load()
        assert res["status"] == "failed"

    def test_empty_dirs_are_preserved(self):
        working_tree = self.make_branch_and_tree(".")

        dirs_and_files = [
            "foo/",
            "foo/foo.py",
            "bar/",
            "bar/bar.py",
            "baz/",
            "foobar",
        ]
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

    @pytest.fixture(autouse=True)
    def git_server(self):
        """Create a simple git repository containing one file.
        This repository will be added as a submodule in another git repository.
        As git forbids to add a submodule from the local filesystem, we serve
        the repository using TCP."""
        first_git_repo_path = os.path.join(self.tmp_path, "first_git_repo")
        first_git_repo = Repo.init(first_git_repo_path, mkdir=True)

        with open(os.path.join(first_git_repo_path, "file"), "w") as f:
            f.write("foo")

        first_git_repo.stage(["file"])
        first_git_repo.do_commit(
            b"file added",
            committer=b"Test Committer <test@example.org>",
            author=b"Test Author <test@example.org>",
            commit_timestamp=12395,
            commit_timezone=0,
            author_timestamp=12395,
            author_timezone=0,
        )

        backend = DictBackend({b"/": first_git_repo})
        git_server = TCPGitServer(backend, b"localhost", 0)

        git_server_thread = threading.Thread(target=git_server.serve)
        git_server_thread.start()

        _, port = git_server.socket.getsockname()
        self.git_server_url = f"git://localhost:{port}/"

        yield

        git_server.shutdown()
        git_server.server_close()
        git_server_thread.join()

    def test_nested_tree_handling(self):
        # create a new git repository
        git_repo_path = os.path.join(self.tmp_path, "git_repo")
        run(["git", "init", git_repo_path], check=True)

        # add the repository served by the git_server fixture as a
        # submodule in it
        run(
            ["git", "submodule", "add", self.git_server_url, "submodule"],
            check=True,
            cwd=git_repo_path,
        )
        run(
            ["git", "commit", "-m", "submodule added"],
            check=True,
            cwd=git_repo_path,
        )

        # create a bazaar repository from the git repository created above
        self.disable_directory_isolation()
        self.run_bzr(f"git-import file://{git_repo_path} bzr_repo")
        # ensure it has a single branch
        self.run_bzr("remove-branch master", working_dir="bzr_repo")

        # load bazaar repository
        repo_url = self.get_url() + "/bzr_repo"
        loader = BazaarLoader(
            self.swh_storage, repo_url, directory=repo_url, check_revision=1
        )
        assert loader.load() == {"status": "eventful"}

        # check submodule has been imported as an empty directory
        snapshot = snapshot_get_latest(self.swh_storage, repo_url)
        swh_rev_id = snapshot.branches[b"trunk"].target
        swh_rev = self.swh_storage.revision_get([swh_rev_id])[0]
        swh_root_dir_entries = list(
            self.swh_storage.directory_ls(swh_rev.directory, recursive=True)
        )
        submodule_entry = [
            entry
            for entry in swh_root_dir_entries
            if entry["type"] == "dir" and entry["name"] == b"submodule"
        ]
        assert submodule_entry
        assert submodule_entry[0]["target"] == Directory(entries=()).id
