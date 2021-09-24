# Copyright (C) 2021-2022  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

"""This document contains a SWH loader for ingesting repository data
from Bazaar or Breezy.
"""
from datetime import datetime
from functools import lru_cache, partial
import os
from tempfile import mkdtemp
from typing import Dict, Iterator, List, NewType, Optional, Set, TypeVar, Union

from breezy import errors as bzr_errors
from breezy import repository, tsort
from breezy.builtins import cmd_clone
from breezy.bzr import bzrdir
from breezy.bzr.branch import Branch as BzrBranch
from breezy.bzr.inventory import Inventory, InventoryEntry
from breezy.revision import NULL_REVISION
from breezy.revision import Revision as BzrRevision

from swh.loader.core.loader import BaseLoader
from swh.loader.core.utils import clean_dangling_folders, clone_with_timeout
from swh.model import from_disk
from swh.model.model import (
    Content,
    ExtID,
    ObjectType,
    Origin,
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
from swh.storage.interface import StorageInterface

TEMPORARY_DIR_PREFIX_PATTERN = "swh.loader.bzr.from_disk"
EXTID_TYPE = "bzr-nodeid"
EXTID_VERSION: int = 1

BzrRevisionId = NewType("BzrRevisionId", bytes)

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


class RepositoryNeedsUpgrade(Exception):
    """The repository we're trying to load is using an old format.
    We only support format 2a (the most recent), see `brz help upgrade`"""


class UnknownRepositoryFormat(Exception):
    """The repository we're trying to load is using an unknown format.
    It's possible (though unlikely) that a new format has come out, we should
    check before dismissing the repository as broken or unsupported."""


class BzrDirectory(from_disk.Directory):
    """A more practical directory.

    - creates missing parent directories
    - removes empty directories
    """

    def __setitem__(
        self, path: bytes, value: Union[from_disk.Content, "BzrDirectory"]
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

    def __delitem__(self, path: bytes) -> None:
        super().__delitem__(path)

        while b"/" in path:  # remove empty parent directories
            path = path.rsplit(b"/", 1)[0]
            if len(self[path]) == 0:
                super().__delitem__(path)
            else:
                break

    def get(
        self, path: bytes, default: Optional[T] = None
    ) -> Optional[Union[from_disk.Content, "BzrDirectory", T]]:
        # TODO move to swh.model.from_disk.Directory
        try:
            return self[path]
        except KeyError:
            return default


class BazaarLoader(BaseLoader):
    """Loads a Bazaar repository"""

    visit_type = "bzr"

    def __init__(
        self,
        storage: StorageInterface,
        url: str,
        directory: Optional[str] = None,
        logging_class: str = "swh.loader.bzr.Loader",
        visit_date: Optional[datetime] = None,
        temp_directory: str = "/tmp",
        clone_timeout_seconds: int = 7200,
        max_content_size: Optional[int] = None,
    ):
        super().__init__(
            storage=storage,
            logging_class=logging_class,
            max_content_size=max_content_size,
        )

        self._temp_directory = temp_directory
        self._clone_timeout = clone_timeout_seconds
        self._revision_id_to_sha1git: Dict[BzrRevisionId, Sha1Git] = {}
        self._last_root = BzrDirectory()
        self._tags: Optional[Dict[bytes, BzrRevisionId]] = None
        self._head_revision_id: Optional[bytes] = None
        self._branch: Optional[BzrBranch] = None
        # Revisions that are pointed to, but don't exist in the current branch
        # Rare, but exist usually for cross-VCS references.
        self._ghosts: Set[BzrRevisionId] = set()
        self._load_status = "eventful"

        self.origin_url = url
        self.visit_date = visit_date
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

    def prepare_origin_visit(self) -> None:
        """First step executed by the loader to prepare origin and visit
        references. Set/update self.origin, and
        optionally self.origin_url, self.visit_date.

        """
        self.origin = Origin(url=self.origin_url)

    def prepare(self) -> None:
        """Second step executed by the loader to prepare some state needed by
        the loader.
        """

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

    def cleanup(self) -> None:
        if self.repo is not None:
            self.repo.unlock()

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
                msg, self.origin_url, self._repo_directory, self._clone_timeout
            )
            closure = partial(cmd_clone().run, self.origin_url, self._repo_directory)
            clone_with_timeout(
                self.origin_url, self._repo_directory, closure, self._clone_timeout
            )
        else:  # existing local repository
            # Allow to load on disk repository without cloning
            # for testing purpose.
            self.log.debug("Using local directory '%s'", self.directory)
            self._repo_directory = self.directory

        res = bzrdir.BzrDir.open_containing_tree_branch_or_repository(
            self._repo_directory
        )
        (_tree, _branch, repo, _relpath) = res
        repository_format = repo._format.as_string()  # lies about being a string
        if not repository_format == expected_repository_format:
            if repository_format in older_repository_formats:
                raise RepositoryNeedsUpgrade()
            else:
                raise UnknownRepositoryFormat()

        self.repo = repo
        self.repo.lock_read()
        self.head_revision_id  # set the property
        self.tags  # set the property
        return False

    def store_data(self):
        """Store fetched data in the database."""
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

        snapshot_branches: Dict[bytes, SnapshotBranch] = {}

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
            target = self._get_revision_id_from_bzr_id(target)
            snapshot_branches[label] = SnapshotBranch(
                target=self.store_release(tag_name, target),
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
                target=b"trunk", target_type=TargetType.ALIAS,
            )

        snapshot = Snapshot(branches=snapshot_branches)
        self.storage.snapshot_add([snapshot])

        self.flush()
        self.loaded_snapshot_id = snapshot.id

    def store_revision(self, bzr_rev: BzrRevision):
        self.log.debug("Storing revision '%s'", bzr_rev.revision_id)
        directory = self.store_directories(bzr_rev)
        associated_bugs = [
            (b"bug", b"%s %s" % (status.encode(), url.encode()))
            for url, status in bzr_rev.iter_bugs()
        ]
        extra_headers = [
            (b"time_offset_seconds", str(bzr_rev.timezone).encode(),),
            *associated_bugs,
        ]
        timestamp = Timestamp(int(bzr_rev.timestamp), 0)
        timezone = round(int(bzr_rev.timezone) / 60)
        date = TimestampWithTimezone.from_numeric_offset(timestamp, timezone, False)

        # TODO (how) should we store multiple authors? (T3887)
        revision = Revision(
            author=Person.from_fullname(bzr_rev.get_apparent_authors()[0].encode()),
            date=date,
            committer=Person.from_fullname(bzr_rev.committer.encode()),
            committer_date=date,
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
        repo: repository.Repository = self.repo
        inventory: Inventory = repo.get_inventory(bzr_rev.revision_id)
        self._store_directories_slow(bzr_rev, inventory)
        return self._store_tree(inventory)

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
        self, bzr_rev: BzrRevision, file_path: str, entry: InventoryEntry
    ) -> from_disk.Content:
        if entry.executable:
            perms = from_disk.DentryPerms.executable_content
        elif entry.kind == "directory":
            perms = from_disk.DentryPerms.directory
        elif entry.kind == "symlink":
            perms = from_disk.DentryPerms.symlink
        elif entry.kind == "file":
            perms = from_disk.DentryPerms.content
        else:  # pragma: no cover
            raise RuntimeError("Hit unreachable condition")

        data = b""
        if entry.has_text():
            rev_tree = self._get_revision_tree(bzr_rev.revision_id)
            data = rev_tree.get_file(file_path).read()
            assert len(data) == entry.text_size

        content = Content.from_data(data)

        self.storage.content_add([content])

        return from_disk.Content({"sha1_git": content.sha1_git, "perms": perms})

    def _get_bzr_revs_to_load(self) -> List[BzrRevision]:
        assert self.repo is not None
        repo: repository.Repository = self.repo
        self.log.debug("Getting fully sorted revision tree")
        if self.head_revision_id == NULL_REVISION:
            return []
        head_revision = repo.get_revision(self.head_revision_id)
        # bazaar's model doesn't allow it to iterate on its graph from
        # the bottom lazily, but basically all DAGs (especially bzr ones)
        # are small enough to fit in RAM.
        ancestors_iter = self._iterate_ancestors(head_revision)
        ancestry = []
        for rev, parents in ancestors_iter:
            if parents is None:
                # Filter out ghosts, they scare the `TopoSorter`.
                # Store them to later catch exceptions about missing parent revision
                self._ghosts.add(rev)
                continue
            ancestry.append((rev, parents))

        sorter = tsort.TopoSorter(ancestry)
        return sorter.sorted()

    def _iterate_ancestors(self, rev: BzrRevision) -> Iterator[BzrRevisionId]:
        """Return an iterator of this revision's ancestors"""
        assert self.repo is not None
        return self.repo.get_graph().iter_ancestry([rev.revision_id])

    @lru_cache()
    def _get_revision_tree(self, rev: BzrRevisionId):
        assert self.repo is not None
        return self.repo.revision_tree(rev)

    def _store_tree(self, inventory: Inventory) -> Sha1Git:
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
        self._prev_inventory = inventory
        return self._last_root.hash

    def _store_directories_slow(self, bzr_rev: BzrRevision, inventory: Inventory):
        """Store a revision directories given its hg nodeid.

        This is the slow variant: it does not use a diff from the last revision
        but lists all the files. A future patch will introduce a faster version.
        """
        # Don't reuse the last root, we're listing everything anyway, and we
        # could be keeping around deleted files
        self._last_root = BzrDirectory()
        for path, entry in inventory.iter_entries():
            if path == "":
                # root repo is created by default
                continue
            content = self.store_content(bzr_rev, path, entry)
            self._last_root[path.encode()] = content

    def _get_revision_parents(self, bzr_rev: BzrRevision):
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
        return self._revision_id_to_sha1git[bzr_id]

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
    def head_revision_id(self) -> bytes:
        """Returns the Bazaar revision id of the branch's head.

        Bazaar/Breezy branches do not have multiple heads."""
        assert self.repo is not None
        if self._head_revision_id is None:
            self._head_revision_id = self.branch.last_revision()
        return self._head_revision_id

    @property
    def tags(self) -> Optional[Dict[bytes, BzrRevisionId]]:
        assert self.repo is not None
        if self._tags is None and self.branch.supports_tags():
            self._tags = {
                n.encode(): r for n, r in self.branch.tags.get_tag_dict().items()
            }
        return self._tags
