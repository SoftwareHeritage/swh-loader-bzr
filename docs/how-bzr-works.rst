Software Heritage - How Bazaar/Breezy works
===========================================

In Bazaar, a repository is simply the store of revisions. It's a storing backend and does not have to carry any semantic purpose for the project(s) it's holding. What users are really dealing with are branches.

A branch is an ordered set of revisions that describes the history of a set of files. It corresponds to a folder on the file system, and can only have a single head: if two clones of a branch diverge, the only way of uniting them is by merging one into the other. A branch needs to have a repository to store its revisions, but multiple branches can share the same repository.

Bazaar does not have a very strong opinion of how it should be used and supports multiple different workflows, even a centralized one with bound branches. We need to pick the most "workflow-agnostic" way of saving Bazaar repositories... or rather branches.

For our purposes, we will treat each branch as a separate origin, since we have no way of knowing if branches inside a repository are related in bzr terms, and also because we de-duplicate on the SWH side:

    - From a user standpoint, they will most likely be searching by branch. If they search by shared repository, they will search with a prefix of a branch, which should also work
    - Since bzr branches do *not* have multiple heads, we don't have to worry about any sort of mapping, we will simply have HEAD
    - Tags are per-branch, so that also works
    - Ghost revisions can be saved even if we don't have the target revision since that's how the `nixguix` loader does it

Not resolved yet:

    - We have to look for stacked branches, how they work and if we can do anything more interesting than just failing.
    - Bazaar is able to store empty directories, does SWH handle them?
    - What do we do about multiple authors (they are line separated) in each commit?
    - What do we do about bug fixes metadata in each commit?
    - What do we do about branch config?
