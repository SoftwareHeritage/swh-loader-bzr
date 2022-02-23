#!/usr/bin/env bash
# Creates a repository with the correct recent format but an old branch
# format that does not support tags

set -euo pipefail
bzr init-shared-repo does-not-support-tags-repo
cd does-not-support-tags-repo
bzr init --knit does-not-support-tags-branch
cd ..