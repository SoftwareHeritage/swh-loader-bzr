# Copyright (C) 2021-2023  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

"""This document contains a SWH loader for ingesting repository data
from Bazaar or Breezy.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache, partial
import itertools
import os
from tempfile import mkdtemp
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, TypeVar, Union

from breezy import errors as bzr_errors
from breezy import repository, tsort
from breezy.branch import Branch as BzrBranch
from breezy.builtins import cmd_branch, cmd_upgrade
from breezy.controldir import ControlDir
from breezy.revision import NULL_REVISION
from breezy.revision import Revision as BzrRevision
from breezy.revision import RevisionID as BzrRevisionId
from breezy.tree import Tree, TreeChange

from swh.loader.core.loader import BaseLoader
from swh.loader.core.utils import clean_dangling_folders, clone_with_timeout
from swh.model import from_disk, swhids
from swh.model.model import (
    Content,
    ExtID,
    ObjectType,
    Person,
    Release,
    Revision,
    RevisionType,
    Sha1Git,
    Snapshot,
    SnapshotBranch,
    TargetType,
    Timestamp,
    TimestampWithTimezone,
)
from swh.storage.algos.snapshot import snapshot_get_latest
from swh.storage.interface import StorageInterface

TEMPORARY_DIR_PREFIX_PATTERN = "swh.loader.bzr.from_disk"
EXTID_TYPE = "bzr-nodeid"
EXTID_VERSION: int = 1

T = TypeVar("T")

# These are all the old Bazaar repository formats that we might encounter
# in the wild. Bazaar's `clone` does not result in an upgrade, it needs to be
# explicit.
older_repository_formats = {
    b"Bazaar Knit Repository Format 3 (bzr 0.15)\n",
    b"Bazaar Knit Repository Format 4 (bzr 1.0)\n",
    b"Bazaar RepositoryFormatKnitPack5 (bzr 1.6)\n",
    b"Bazaar RepositoryFormatKnitPack5RichRoot (bzr 1.6)\n",
    b"Bazaar RepositoryFormatKnitPack5RichRoot (bzr 1.6.1)\n",
    b"Bazaar RepositoryFormatKnitPack6 (bzr 1.9)\n",
    b"Bazaar RepositoryFormatKnitPack6RichRoot (bzr 1.9)\n",
    b"Bazaar development format 2 with subtree support \
        (needs bzr.dev from before 1.8)\n",
    b"Bazaar development format 8\n",
    b"Bazaar pack repository format 1 (needs bzr 0.92)\n",
    b"Bazaar pack repository format 1 with rich root (needs bzr 1.0)\n",
    b"Bazaar pack repository format 1 with subtree support (needs bzr 0.92)\n",
    b"Bazaar-NG Knit Repository Format 1",
}

# Latest one as of this time, unlikely to change
expected_repository_format = b"Bazaar repository format 2a (needs bzr 1.16 or later)\n"


class UnknownRepositoryFormat(Exception):
    """The repository we're trying to load is using an unknown format.
    It's possible (though unlikely) that a new format has come out, we should
    check before dismissing the repository as broken or unsupported."""


class BzrDirectory(from_disk.Directory):
    """A more practical directory to create missing parent directories
    when adding a path.
    """

    def __setitem__(
        self, path: bytes, value: Union[from_disk.Content, BzrDirectory]
    ) -> None:
        if b"/" in path:
            head, tail = path.split(b"/", 1)

            directory = self.get(head)
            if directory is None or isinstance(directory, from_disk.Content):
                directory = BzrDirectory()
                self[head] = directory

            directory[tail] = value
        else:
            super().__setitem__(path, value)

    def get(
        self, path: bytes, default: Optional[T] = None
    ) -> Optional[Union[from_disk.Content, BzrDirectory, T]]:
        # TODO move to swh.model.from_disk.Directory
        try:
            return self[path]
        except KeyError:
            return default


def sort_changes(change: TreeChange) -> str:
    """Key function for sorting the changes by path.

    Sorting allows us to group the folders together (for example "b", then "a/a",
    then "a/b"). Reversing this sort in the `sorted()` call will make it
    so the files appear before the folder ("a/a", then "a") if the folder has
    changed. This removes a bug where the order of operations is:

        - "a" goes from directory to file, removing all of its subtree
        - "a/a" is removed, but our structure has already forgotten it"""
    source_path, target_path = change.path
    # Neither path can be the empty string
    return source_path or target_path


class BazaarLoader(BaseLoader):
    """Loads a Bazaar repository"""

    visit_type = "bzr"

    def __init__(
        self,
        storage: StorageInterface,
        url: str,
        directory: Optional[str] = None,
        visit_date: Optional[datetime] = None,
        temp_directory: str = "/tmp",
        clone_timeout_seconds: int = 7200,
        **kwargs: Any,
    ):
        super().__init__(storage=storage, origin_url=url, **kwargs)

        self._temp_directory = temp_directory
        self._clone_timeout = clone_timeout_seconds
        self._revision_id_to_sha1git: Dict[BzrRevisionId, Sha1Git] = {}
        self._last_root = BzrDirectory()
        self._tags: Optional[Dict[bytes, BzrRevisionId]] = None
        self._head_revision_id: Optional[bytes] = None
        # Remember the previous revision to only compute the delta between
        # revisions
        self._prev_revision: Optional[BzrRevision] = None
        self._branch: Optional[BzrBranch] = None
        # Revisions that are pointed to, but don't exist in the current branch
        # Rare, but exist usually for cross-VCS references.
        self._ghosts: Set[BzrRevisionId] = set()
        # Exists if in an incremental run, is the latest saved revision from
        # this origin
        self._latest_head: Optional[BzrRevisionId] = None
        self._load_status = "eventful"

        self.visit_date = visit_date or self.visit_date
        self.directory = directory
        self.repo: Optional[repository.Repository] = None

    def pre_cleanup(self) -> None:
        """As a first step, will try and check for dangling data to cleanup.
        This should do its best to avoid raising issues.

        """
        clean_dangling_folders(
            self._temp_directory,
            pattern_check=TEMPORARY_DIR_PREFIX_PATTERN,
            log=self.log,
        )

    def prepare(self) -> None:
        """Second step executed by the loader to prepare some state needed by
        the loader.
        """
        latest_snapshot = snapshot_get_latest(self.storage, self.origin.url)
        if latest_snapshot:
            self._set_recorded_state(latest_snapshot)

    def load_status(self) -> Dict[str, str]:
        """Detailed loading status.

        Defaults to logging an eventful load.

        Returns: a dictionary that is eventually passed back as the task's
          result to the scheduler, allowing tuning of the task recurrence
          mechanism.
        """
        return {
            "status": self._load_status,
        }

    def _set_recorded_state(self, latest_snapshot: Snapshot) -> None:
        if not latest_snapshot.branches:
            # Last snapshot was empty
            return
        head = latest_snapshot.branches[b"trunk"]
        bzr_head = self._get_extids_for_targets([head.target])[0].extid
        self._latest_head = BzrRevisionId(bzr_head)

    def _get_extids_for_targets(self, targets: List[Sha1Git]) -> List[ExtID]:
        """Get all Bzr ExtIDs for the targets in the latest snapshot"""
        extids = []
        for extid in self.storage.extid_get_from_target(
            swhids.ObjectType.REVISION,
            targets,
            extid_type=EXTID_TYPE,
            extid_version=EXTID_VERSION,
        ):
            extids.append(extid)
            self._revision_id_to_sha1git[
                BzrRevisionId(extid.extid)
            ] = extid.target.object_id

        if extids:
            # Filter out dangling extids, we need to load their target again
            revisions_missing = self.storage.revision_missing(
                [extid.target.object_id for extid in extids]
            )
            extids = [
                extid
                for extid in extids
                if extid.target.object_id not in revisions_missing
            ]
        return extids

    def cleanup(self) -> None:
        if self.repo is not None:
            self.repo.unlock()

    def get_repo_and_branch(self) -> Tuple[repository.Repository, BzrBranch]:
        _, branch, repo, _ = ControlDir.open_containing_tree_branch_or_repository(
            self._repo_directory
        )
        return repo, branch

    def run_upgrade(self):
        """Upgrade both repository and branch to the most recent supported version
        to be compatible with the loader."""
        cmd_upgrade().run(self._repo_directory, clean=True)

    def fetch_data(self) -> bool:
        """Fetch the data from the source the loader is currently loading

        Returns:
            a value that is interpreted as a boolean. If True, fetch_data needs
            to be called again to complete loading.

        """
        if not self.directory:  # no local repository
            self._repo_directory = mkdtemp(
                prefix=TEMPORARY_DIR_PREFIX_PATTERN,
                suffix=f"-{os.getpid()}",
                dir=self._temp_directory,
            )
            msg = "Cloning '%s' to '%s' with timeout %s seconds"
            self.log.debug(
                msg, self.origin.url, self._repo_directory, self._clone_timeout
            )
            closure = partial(
                cmd_branch().run,
                self.origin.url,
                self._repo_directory,
                no_tree=True,
                use_existing_dir=True,
            )
            clone_with_timeout(
                self.origin.url, self._repo_directory, closure, self._clone_timeout
            )
        else:  # existing local repository
            # Allow to load on disk repository without cloning
            # for testing purpose.
            self.log.debug("Using local directory '%s'", self.directory)
            self._repo_directory = self.directory

        repo, branch = self.get_repo_and_branch()
        repository_format = repo._format.get_format_string()

        if not repository_format == expected_repository_format:
            if repository_format in older_repository_formats:
                self.log.debug(
                    "Upgrading repository from format '%s'",
                    repository_format.decode("ascii").strip("\n"),
                )
                self.run_upgrade()
                repo, branch = self.get_repo_and_branch()
            else:
                raise UnknownRepositoryFormat()

        if not branch.supports_tags():
            # Some repos have the right format marker but their branches do not
            # support tags
            self.log.debug("Branch does not support tags, upgrading")
            self.run_upgrade()
            repo, branch = self.get_repo_and_branch()
            # We could set the branch here directly, but we want to run the
            # sanity checks in the `self.branch` property, so let's make sure
            # we invalidate the "cache".
            self._branch = None

        self.repo = repo
        self.repo.lock_read()
        self.head_revision_id  # set the property
        self.tags  # set the property
        return False

    def store_data(self) -> None:
        """Store fetched data in the database."""
        assert self.repo is not None
        assert self.tags is not None

        # Insert revisions using a topological sorting
        revs = self._get_bzr_revs_to_load()

        if revs and revs[0] == NULL_REVISION:
            # The first rev we load isn't necessarily `NULL_REVISION` even in a
            # full load, as bzr allows for ghost revisions.
            revs = revs[1:]

        length_ingested_revs = 0
        for rev in revs:
            self.store_revision(self.repo.get_revision(rev))
            length_ingested_revs += 1

        if length_ingested_revs == 0:
            # no new revision ingested, so uneventful
            # still we'll make a snapshot, so we continue
            self._load_status = "uneventful"

        snapshot_branches: Dict[bytes, Optional[SnapshotBranch]] = {}

        for tag_name, target in self.tags.items():
            label = b"tags/%s" % tag_name
            if target == NULL_REVISION:
                # Some very rare repositories have meaningless tags that point
                # to the null revision.
                self.log.debug("Tag '%s' points to the null revision", tag_name)
                snapshot_branches[label] = None
                continue
            try:
                # Used only to detect corruption
                self.branch.revision_id_to_dotted_revno(target)
            except (
                bzr_errors.NoSuchRevision,
                bzr_errors.GhostRevisionsHaveNoRevno,
                bzr_errors.UnsupportedOperation,
            ):
                # Bad tag data/merges can lead to tagged revisions
                # which are not in this branch. We cannot point a tag there.
                snapshot_branches[label] = None
                continue
            snp_target = self._get_revision_id_from_bzr_id(target)
            snapshot_branches[label] = SnapshotBranch(
                target=self.store_release(tag_name, snp_target),
                target_type=TargetType.RELEASE,
            )

        if self.head_revision_id != NULL_REVISION:
            head_revision_git_hash = self._get_revision_id_from_bzr_id(
                self.head_revision_id
            )
            snapshot_branches[b"trunk"] = SnapshotBranch(
                target=head_revision_git_hash, target_type=TargetType.REVISION
            )
            snapshot_branches[b"HEAD"] = SnapshotBranch(
                target=b"trunk",
                target_type=TargetType.ALIAS,
            )

        snapshot = Snapshot(branches=snapshot_branches)
        self.storage.snapshot_add([snapshot])

        self.flush()
        self.loaded_snapshot_id = snapshot.id

    def store_revision(self, bzr_rev: BzrRevision) -> None:
        self.log.debug("Storing revision '%s'", bzr_rev.revision_id)
        directory = self.store_directories(bzr_rev)
        associated_bugs = [
            (b"bug", b"%s %s" % (status.encode(), url.encode()))
            for url, status in bzr_rev.iter_bugs()
        ]
        extra_headers = [
            (
                b"time_offset_seconds",
                str(bzr_rev.timezone).encode(),
            ),
            *associated_bugs,
        ]
        timestamp = Timestamp(int(bzr_rev.timestamp), 0)
        timezone = round(int(bzr_rev.timezone) / 60)
        date = TimestampWithTimezone.from_numeric_offset(timestamp, timezone, False)

        committer = (
            Person.from_fullname(bzr_rev.committer.encode())
            if bzr_rev.committer is not None
            else None
        )

        # TODO (how) should we store multiple authors? (T3887)
        revision = Revision(
            author=Person.from_fullname(bzr_rev.get_apparent_authors()[0].encode()),
            date=date,
            committer=committer,
            committer_date=date if committer is not None else None,
            type=RevisionType.BAZAAR,
            directory=directory,
            message=bzr_rev.message.encode(),
            extra_headers=extra_headers,
            synthetic=False,
            parents=self._get_revision_parents(bzr_rev),
        )

        self._revision_id_to_sha1git[bzr_rev.revision_id] = revision.id
        self.storage.revision_add([revision])

        self.storage.extid_add(
            [
                ExtID(
                    extid_type=EXTID_TYPE,
                    extid_version=EXTID_VERSION,
                    extid=bzr_rev.revision_id,
                    target=revision.swhid(),
                )
            ]
        )

    def store_directories(self, bzr_rev: BzrRevision) -> Sha1Git:
        """Store a revision's directories."""
        new_tree = self._get_revision_tree(bzr_rev.revision_id)
        if self._prev_revision is None:
            self._store_directories_slow(bzr_rev, new_tree)
            return self._store_tree(bzr_rev)

        old_tree = self._get_revision_tree(self._prev_revision.revision_id)

        delta = new_tree.changes_from(old_tree)

        if delta.renamed or delta.copied:
            # Figuring out all nested and possibly conflicting renames is a lot
            # of effort for very few revisions, just go the slow way
            self._store_directories_slow(bzr_rev, new_tree)
            return self._store_tree(bzr_rev)

        to_remove = sorted(
            delta.removed + delta.missing, key=sort_changes, reverse=True
        )
        for change in to_remove:
            path = change.path[0]
            del self._last_root[path.encode()]

        # `delta.kind_changed` needs to happen before `delta.added` since a file
        # could be added under a node that changed from directory to file at the
        # same time, for example
        for change in itertools.chain(delta.kind_changed, delta.added, delta.modified):
            path = change.path[1]
            (kind, size, executable, _sha1_or_link) = new_tree.path_content_summary(
                path
            )
            dir_entry: Union[BzrDirectory, from_disk.Content]
            if kind in ("file", "symlink"):
                dir_entry = self.store_content(
                    bzr_rev,
                    path,
                    kind,
                    executable,
                    size,
                    _sha1_or_link if kind == "symlink" else None,
                )
            elif kind == "directory":
                dir_entry = BzrDirectory()
            else:
                raise RuntimeError(
                    f"Unable to process directory entry {path} of kind {kind}"
                )

            self._last_root[path.encode()] = dir_entry

        self._prev_revision = bzr_rev
        return self._store_tree(bzr_rev)

    def store_release(self, name: bytes, target: Sha1Git) -> Sha1Git:
        """Store a release given its name and its target.

        Args:
            name: name of the release.
            target: sha1_git of the target revision.

        Returns:
            the sha1_git of the stored release.
        """
        release = Release(
            name=name,
            target=target,
            target_type=ObjectType.REVISION,
            message=None,
            metadata=None,
            synthetic=False,
            author=Person(name=None, email=None, fullname=b""),
            date=None,
        )

        self.storage.release_add([release])

        return release.id

    def store_content(
        self,
        bzr_rev: BzrRevision,
        file_path: str,
        kind: str,
        executable: bool,
        size: int,
        symlink_target: Optional[str] = None,
    ) -> from_disk.Content:
        assert kind in ("file", "symlink")
        if executable:
            perms = from_disk.DentryPerms.executable_content
        elif kind == "symlink":
            perms = from_disk.DentryPerms.symlink
        else:  # kind == "file":
            perms = from_disk.DentryPerms.content

        if kind == "file":
            rev_tree = self._get_revision_tree(bzr_rev.revision_id)
            with rev_tree.get_file(file_path) as f:
                data = f.read()
            assert len(data) == size
        else:  # kind == "symlink":
            assert symlink_target is not None
            data = symlink_target.encode()

        content = Content.from_data(data)

        self.storage.content_add([content])

        return from_disk.Content({"sha1_git": content.sha1_git, "perms": perms})

    def _get_bzr_revs_to_load(self) -> List[BzrRevisionId]:
        assert self.repo is not None
        self.log.debug("Getting fully sorted revision tree")
        if self.head_revision_id == NULL_REVISION:
            return []
        # bazaar's model doesn't allow it to iterate on its graph from
        # the bottom lazily, but basically all DAGs (especially bzr ones)
        # are small enough to fit in RAM.
        ancestors_iter = self._iterate_ancestors(self.head_revision_id)
        ancestry = []
        for rev, parents in ancestors_iter:
            if parents is None:
                # Filter out ghosts, they scare the `TopoSorter`.
                # Store them to later catch exceptions about missing parent revision
                self._ghosts.add(rev)
                continue
            ancestry.append((rev, parents))

        sorter = tsort.TopoSorter(ancestry)
        all_revisions = sorter.sorted()
        if self._latest_head is not None:
            # Breezy does not offer a generic querying system, so we do the
            # filtering ourselves, which is simple enough given that bzr does
            # not have multiple heads per branch
            found = False
            new_revisions = []
            # Filter out revisions until we reach the one we've already seen
            for rev in all_revisions:
                if not found:
                    if rev == self._latest_head:
                        found = True
                else:
                    new_revisions.append(rev)
            if not found and all_revisions:
                # The previously saved head has been uncommitted, reload
                # everything
                msg = "Previous head (%s) not found, loading all revisions"
                self.log.debug(msg, self._latest_head)
                return all_revisions
            return new_revisions
        return all_revisions

    def _iterate_ancestors(
        self, revision_id: BzrRevisionId
    ) -> Iterator[Tuple[BzrRevisionId, Tuple[BzrRevisionId]]]:
        """Return an iterator of this revision's ancestors"""
        assert self.repo is not None
        return self.repo.get_graph().iter_ancestry([revision_id])

    # We want to cache at most the current revision and the last, no need to
    # take cache more than this.
    @lru_cache(maxsize=2)
    def _get_revision_tree(self, rev: BzrRevisionId) -> Tree:
        assert self.repo is not None
        return self.repo.revision_tree(rev)

    def _store_tree(self, bzr_rev: BzrRevision) -> Sha1Git:
        """Save the current in-memory tree to storage."""
        directories: List[from_disk.Directory] = [self._last_root]
        while directories:
            directory = directories.pop()
            self.storage.directory_add([directory.to_model()])
            directories.extend(
                [
                    item
                    for item in directory.values()
                    if isinstance(item, from_disk.Directory)
                ]
            )
        self._prev_revision = bzr_rev
        return self._last_root.hash

    def _store_directories_slow(self, bzr_rev: BzrRevision, tree: Tree) -> None:
        """Store a revision's directories.

        This is the slow variant: it does not use a diff from the last revision
        but lists all the files. It is used for the first revision of a load
        (the null revision for a full run, the last recorded head for an
        incremental one) or for cases where the headaches of figuring out the
        delta from the breezy primitives is not worth it.
        """
        # Don't reuse the last root, we're listing everything anyway, and we
        # could be keeping around deleted files
        self._last_root = BzrDirectory()
        for path, entry in tree.iter_entries_by_dir():
            if path == "":
                # root repo is created by default
                continue

            dir_entry: Union[BzrDirectory, from_disk.Content]
            if entry.kind in ("file", "symlink"):
                dir_entry = self.store_content(
                    bzr_rev,
                    path,
                    entry.kind,
                    entry.executable,
                    entry.text_size,
                    entry.symlink_target if entry.kind == "symlink" else None,
                )
            elif entry.kind == "directory":
                dir_entry = BzrDirectory()
            else:
                raise RuntimeError(
                    f"Unable to process directory entry {path} of kind {entry.kind}"
                )

            self._last_root[path.encode()] = dir_entry

    def _get_revision_parents(self, bzr_rev: BzrRevision) -> Tuple[Sha1Git, ...]:
        parents = []
        for parent_id in bzr_rev.parent_ids:
            if parent_id == NULL_REVISION:
                # Paranoid, don't think that actually happens
                continue
            try:
                revision_id = self._get_revision_id_from_bzr_id(parent_id)
            except LookupError:
                if parent_id in self._ghosts:
                    # We can't store ghosts in any meaningful way (yet?). They
                    # have no contents by definition, and they're pretty rare,
                    # so just ignore them.
                    continue
                raise
            parents.append(revision_id)

        return tuple(parents)

    def _get_revision_id_from_bzr_id(self, bzr_id: BzrRevisionId) -> Sha1Git:
        """Return the git sha1 of a revision given its bazaar revision id."""
        from_cache = self._revision_id_to_sha1git.get(bzr_id)
        if from_cache is not None:
            return from_cache
        # The parent was not loaded in this run, get it from storage
        from_storage = self.storage.extid_get_from_extid(
            EXTID_TYPE, ids=[bzr_id], version=EXTID_VERSION
        )

        if len(from_storage) != 1:
            msg = "Expected 1 match from storage for bzr node %r, got %d"
            raise LookupError(msg % (bzr_id.hex(), len(from_storage)))
        return from_storage[0].target.object_id

    @property
    def branch(self) -> BzrBranch:
        """Returns the only branch in the current repository.

        Bazaar branches can be assimilated to repositories in other VCS like
        Git or Mercurial. By contrast, a Bazaar repository is just a store of
        revisions to optimize disk usage, with no particular semantics."""
        assert self.repo is not None
        branches = list(self.repo.find_branches(using=True))
        msg = "Expected only 1 branch in the repository, got %d"
        assert len(branches) == 1, msg % len(branches)
        self._branch = branches[0]
        return branches[0]

    @property
    def head_revision_id(self) -> BzrRevisionId:
        """Returns the Bazaar revision id of the branch's head.

        Bazaar branches do not have multiple heads."""
        assert self.repo is not None
        if self._head_revision_id is None:
            self._head_revision_id = self.branch.last_revision()
        assert self._head_revision_id is not None
        return BzrRevisionId(self._head_revision_id)

    @property
    def tags(self) -> Optional[Dict[bytes, BzrRevisionId]]:
        assert self.repo is not None
        if self._tags is None:
            self._tags = {
                n.encode(): r for n, r in self.branch.tags.get_tag_dict().items()
            }
        return self._tags
