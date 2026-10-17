#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Giovanni Andrea Binda <322129496+giobinda@users.noreply.github.com>
# Usage: run_test.sh WORKDIR OLD_SIZE NEW_SIZE MKFS_ARGS...
# Builds an exFAT image with the kernel driver, grows it, and verifies it.
set -euo pipefail
W=$1; OLD=$2; NEW=$3; shift 3
TOOL="$(cd "$(dirname "$0")/.." && pwd)/exfat_grow.py"
IMG=$W/fs.img
rm -rf "$W"; mkdir -p "$W"

attach() { udisksctl loop-setup --no-user-interaction -f "$IMG" | sed -n 's/.* as \(\/dev\/loop[0-9]*\)\./\1/p'; }
mnt()    { udisksctl mount --no-user-interaction -b "$1" | sed -n 's/.* at \(.*\)$/\1/p'; }
detach() { udisksctl unmount --no-user-interaction -b "$1" >/dev/null; udisksctl loop-delete --no-user-interaction -b "$1"; }

truncate -s "$OLD" "$IMG"
mkfs.exfat -q -L T "$@" "$IMG"
LOOP=$(attach); M=$(mnt "$LOOP")
# contiguous file right at the start of the heap (likely spans the moved region)
head -c 7M /dev/urandom > "$M/first.bin"
mkdir -p "$M/a/b/c/d/e" "$M/many"
for i in $(seq 1 300); do echo "file $i" > "$M/many/f_$i.txt"; done
: > "$M/empty"
touch "$M/a/b/c/d/e/$(printf 'n%.0s' {1..200})"
# fragmented, FAT-chained files: interleaved appends
for i in $(seq 1 60); do
  head -c 37K /dev/urandom >> "$M/frag1"; head -c 53K /dev/urandom >> "$M/a/frag2"; sync
done
# holes, then a file that fills them
for i in $(seq 1 40); do head -c 100K /dev/urandom > "$M/hole_$i"; done; sync
for i in $(seq 1 40 | awk 'NR%2'); do rm "$M/hole_$i"; done; sync
head -c 3M /dev/urandom > "$M/a/b/filler"
head -c 20M /dev/urandom > "$M/a/b/c/big.bin"
(cd "$M" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$W/before.sha"
detach "$LOOP"
fsck.exfat -n "$IMG" >/dev/null
cp --sparse=always "$IMG" "$W/orig.img"

truncate -s "$NEW" "$IMG"
python3 "$TOOL" grow "$IMG" --undo "$W/undo.bin" --write
fsck.exfat -n "$IMG"

LOOP=$(attach); M=$(mnt "$LOOP")
(cd "$M" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$W/after.sha"
diff "$W/before.sha" "$W/after.sha" && echo "CONTENT OK"
df -B1 "$M" | tail -1
# fill the new space and check again
head -c "$(( $(df -B1 --output=avail "$M" | tail -1) - 8*1024*1024 ))" /dev/zero > "$M/fill" || true
sync; df -h "$M" | tail -1
detach "$LOOP"
fsck.exfat -n "$IMG" && echo "FSCK AFTER FILL OK"

# undo test on a fresh copy
cp --sparse=always "$W/orig.img" "$IMG"; truncate -s "$NEW" "$IMG"
python3 "$TOOL" grow "$IMG" --undo "$W/undo2.bin" --write >/dev/null
python3 "$TOOL" restore "$IMG" --undo "$W/undo2.bin" --write >/dev/null
truncate -s "$OLD" "$IMG"
cmp "$IMG" "$W/orig.img" && echo "RESTORE OK (byte-identical)"
rm -f "$W"/*.img
