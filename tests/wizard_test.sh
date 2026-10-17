#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Giovanni Andrea Binda <322129496+giobinda@users.noreply.github.com>
# Usage: wizard_test.sh WORKDIR LABEL(dos|gpt) [--second-partition]
# Partitioned disk image: 1 GiB exFAT partition at sector 2048 on a 3 GiB disk.
# Runs `wizard --yes` on the image, then extracts the partition and verifies it.
set -euo pipefail
W=$1; LABEL=$2; SECOND=${3:-}
TOOL="$(cd "$(dirname "$0")/.." && pwd)/exfat_grow.py"
rm -rf "$W"; mkdir -p "$W"
attach() { udisksctl loop-setup --no-user-interaction -f "$1" | sed -n 's/.* as \(\/dev\/loop[0-9]*\)\./\1/p'; }
mnt()    { udisksctl mount --no-user-interaction -b "$1" | sed -n 's/.* at \(.*\)$/\1/p'; }
detach() { udisksctl unmount --no-user-interaction -b "$1" >/dev/null; udisksctl loop-delete --no-user-interaction -b "$1"; }

truncate -s 1G "$W/fs.img"; mkfs.exfat -q -c 4K -L W "$W/fs.img"
L=$(attach "$W/fs.img"); M=$(mnt "$L")
head -c 9M /dev/urandom > "$M/first.bin"; mkdir -p "$M/x/y"
for i in $(seq 50); do head -c 20K /dev/urandom >> "$M/frag"; head -c 30K /dev/urandom >> "$M/x/y/frag2"; sync; done
(cd "$M" && find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$W/before.sha"
detach "$L"

truncate -s 3G "$W/disk.img"
if [ -n "$SECOND" ]; then
  printf 'label: %s\nstart=2048, size=2097152, type=%s\nstart=4194304, size=1048576\n' "$LABEL" \
    "$([ "$LABEL" = gpt ] && echo EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 || echo 7)" | sfdisk -q "$W/disk.img"
else
  printf 'label: %s\nstart=2048, size=2097152, type=%s\n' "$LABEL" \
    "$([ "$LABEL" = gpt ] && echo EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 || echo 7)" | sfdisk -q "$W/disk.img"
fi
dd if="$W/fs.img" of="$W/disk.img" bs=1M seek=1 conv=notrunc status=none

python3 "$TOOL" wizard "$W/disk.img" --yes --backup-dir "$W"
sfdisk -d "$W/disk.img" | grep -E "start="
read START SIZE < <(sfdisk -J "$W/disk.img" | python3 -c 'import json,sys; p=json.load(sys.stdin)["partitiontable"]["partitions"][0]; print(p["start"], p["size"])')
dd if="$W/disk.img" of="$W/out.img" bs=512 skip=$START count=$SIZE status=none
fsck.exfat -n "$W/out.img" | tail -1
L=$(attach "$W/out.img"); M=$(mnt "$L")
(cd "$M" && find . -type f -print0 | sort -z | xargs -0 sha256sum) | diff "$W/before.sha" - && echo "CONTENT OK"
df -h "$M" | tail -1
detach "$L"
rm -f "$W"/*.img
