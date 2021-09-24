#!/usr/bin/env bash
# Creates a Bazaar repository with a tag pointing to the null revision
# Requires Breezy 3.2+
set -euo pipefail

bzr init broken-tags
cd broken-tags
bzr tag null-tag
cd ..