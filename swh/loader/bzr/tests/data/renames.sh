#!/usr/bin/env bash
# Creates a repository with a lot of rename manipulation
# Requires Breezy 3.2+

set -euo pipefail

bzr init renames
cd renames
mkdir dir1 dir2
touch dir1/file{1,2}
touch dir2/file{3,4}
bzr add *
bzr commit -mInitial
bzr mv dir1 dir1-renamed
bzr mv dir2 dir2-renamed
bzr mv dir1-renamed/file1 dir1-renamed/file1-renamed
bzr commit -mrenames