"""
Creates a repository with a revision based on a ghost revision, as well
as a tag pointing to said ghost.

Requires Breezy 3.2+
"""
from breezy.bzr.bzrdir import BzrDir

if __name__ == "__main__":
    tree = BzrDir.create_standalone_workingtree("ghosts")
    tree.add_pending_merge(b"iamaghostboo")
    tree.commit(message="some commit")
    tree.branch.tags.set_tag("brokentag", b"iamaghostboo")
