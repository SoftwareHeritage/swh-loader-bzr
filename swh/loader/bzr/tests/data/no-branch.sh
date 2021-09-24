#!/usr/bin/env bash
# Creates a repository with no branch
# Requires Breezy 3.2+

set -euo pipefail

bzr init no-branch --1.6.1-rich-root
cd no-branch
bzr upgrade  # don't trigger the repository format check
cd ..