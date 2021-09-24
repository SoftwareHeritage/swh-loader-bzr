#!/usr/bin/env bash
# Creates a nominal repository with a decent amount of normal operations
# Requires Breezy 3.2+

set -euo pipefail

bzr init nominal
cd nominal
echo a > a.txt
mkdir dir
echo "contents\nhere" > dir/b.txt
mkdir empty-dir
echo c > dir/c
echo d > d
bzr add *
bzr commit -m "Initial commit"
cd ..
bzr branch nominal nominal-branch
cd nominal-branch
echo "other text" >> a.txt
bzr add *
bzr commit -m "Modified a \nThis change happened in another branch"
cd ../nominal
bzr merge ../nominal-branch
bzr commit -m merge
ln -s dir link
bzr add *
bzr commit -m "Add symlink"
rm d
bzr commit -m "deleted d"
bzr tag -r 2 0.1
bzr tag -r 2 other-tag
bzr tag -r 4 latest
echo fix-bug >> dir/b.txt
bzr config bugtracker_bz_url="https://bz.example.com/?show_bug={id}"
bzr commit -m "fixing bugs" --fixes lp:1234 --fixes bz:4321