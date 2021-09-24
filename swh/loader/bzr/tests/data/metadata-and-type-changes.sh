#!/usr/bin/env bash
# Creates a repository with various metadata and file type changes
# Requires Breezy 3.2+

set -euo pipefail

bzr init metadata-and-type-changes
cd metadata-and-type-changes
touch a
ln -s a b
mkdir dir
touch dir/{c,d,e}
mkdir dir/dir2
touch dir/dir2/{f,g,h}
bzr add
bzr commit -minitial
rm a b
touch b
mkdir a
touch a/file
mv a/file a/file1
mv dir dir-other
ln -s dir-other dir
bzr add *
bzr commit -mchanges
rm dir-other -r
bzr commit -mremove-dir
chmod +x b
bzr commit -mexecutable
bzr commit -mempty --unchanged
rm -r a
touch a
bzr add a
bzr commit -mdir-to-file
rm dir
mkdir dir
bzr commit -msymlink-to-dir
cd ..