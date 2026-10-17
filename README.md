# exfat-grow

Grow an exFAT filesystem in place on Linux, without reformatting and
without copying your data elsewhere first.

As of this writing no other Linux tool can resize exFAT: `exfatprogs`,
`parted` and GParted all refuse. `exfat-grow` fills that gap for the common
case of making an exFAT partition use the whole disk.

```console
$ python3 exfat_grow.py wizard /dev/sda1
```

The wizard unmounts the partition, checks it, extends the partition to the
end of the free space, grows the filesystem, and verifies the result. It
asks before every step that writes anything.

> **Status:** early (0.1.0). It has been tested on many disk images and
> used successfully on a real 466 GiB → 954 GiB SSD, but it rewrites
> filesystem metadata. **Back up data you cannot afford to lose.**

## Requirements

- Linux, Python ≥ 3.8 (standard library only)
- `exfatprogs` (`fsck.exfat`) and `util-linux` (`sfdisk`, `lsblk`,
  `findmnt`, `partx`)
- root access for real disks (the wizard re-runs itself with `sudo`)

## Installation

The tool is a single file, so it needs no installation:

```console
$ python3 exfat_grow.py --help
```

Or install it as the `exfat-grow` command:

```console
$ pipx install .            # or: pip install --user .
$ exfat-grow --help
```

## Usage

### Guided (recommended)

```console
$ exfat-grow wizard                 # pick from the exFAT partitions found
$ exfat-grow wizard /dev/sda1       # a specific partition
$ exfat-grow wizard /dev/sda1 --yes # unattended: answer yes to everything
```

Steps:

1. **Select**: identify the partition, its disk and partition number.
2. **Unmount**, if mounted (after asking).
3. **Check**: `fsck.exfat -n` plus the tool's own consistency scan. The
   filesystem must be clean. Interactively you can choose to repair it;
   `--yes` never repairs automatically.
4. **Grow the partition**: the old table is saved with `sfdisk -d`, then the
   partition is extended to the largest size possible without moving its
   start or touching later partitions. Works with MBR and GPT primary
   partitions.
5. **Plan**: shows exactly what will be written.
6. **Resize the filesystem**: undo data is saved first.
7. **Verify**: `fsck.exfat -n` and a rescan. On failure the wizard offers to
   restore both the filesystem and the partition table.

Backups go to your home directory (`--backup-dir` to change). The wizard
refuses to put them on the disk being modified.

The wizard also accepts a disk image file (with `--partition N` if it holds
several exFAT partitions) or a bare filesystem image file.

### Individual commands

```console
$ exfat-grow info    /dev/sda1                    # geometry and plan, read-only
$ exfat-grow grow    /dev/sda1                    # dry run
$ exfat-grow grow    /dev/sda1 --write --undo ~/undo.bin
$ exfat-grow restore /dev/sda1 --undo ~/undo.bin --write
```

`grow` expands the filesystem to the size of the device you give it, so
extend the partition first (e.g. `echo ',+' | sfdisk -N 1 /dev/sda`).
`--sectors N` sets a smaller target size.

## How it works

The exFAT layout is: boot regions, FAT, then the cluster heap. Growing the
volume means more clusters, so a bigger FAT (4 bytes per cluster) and a
bigger allocation bitmap.

- The FAT must end before the cluster heap starts. Formatters leave almost
  no slack there, so doubling a volume normally needs the heap start to move
  forward by *k* clusters. `exfat-grow` picks the smallest such *k*,
  copies the data in the first *k* clusters into the newly added space, and
  renumbers every cluster reference: FAT chains, every directory entry's
  first cluster (with entry-set checksums recomputed), the root directory
  pointer and the bitmap. All other file data stays where it is. For a
  466 GiB → 954 GiB volume with 128 KiB clusters that is 119 clusters
  (≈15 MiB).
- A contiguous ("NoFatChain") file that crosses the moved region is
  converted to a FAT-chained file. This is valid exFAT and its contents are
  unchanged.
- A new, larger allocation bitmap is written contiguously in the new
  space, and the old one is freed.
- The up-case table is moved as a whole if needed (the Linux driver reads
  it as a contiguous run).
- New main and backup boot regions are written last, with fresh checksums.

### Safety measures

- Every write is computed in memory and checked against the volume bounds
  before anything is written.
- Before writing, the original contents of every region that will be
  overwritten are saved to an undo file (checksummed with SHA-256, a few
  tens of MB). `restore` writes them back.
- The volume is marked dirty during the operation and only marked clean
  by the final boot region write.
- Refuses to run on a mounted or busy device (`O_EXCL`), a dirty volume,
  a volume with MediaFailure set, cross-linked or unallocated clusters,
  bad checksums, or unknown critical directory entries.

## Limitations

- Grow only; no shrinking.
- Single-FAT volumes only (no TexFAT / two FATs).
- MBR logical (extended) partitions are not supported by the wizard.
- The partition start is never moved.
- Whole metadata is held in memory: fine for typical cluster sizes (a
  1 TB volume with 128 KiB clusters needs well under 100 MB). Very large
  volumes with very small clusters need correspondingly more RAM.
- Not crash-proof: a power loss mid-way leaves a dirty volume that you
  restore from the undo file.

## Testing

The tests build real exFAT images, populate them with the kernel exFAT
driver, grow them, and check the result with `fsck.exfat`, a remount and
SHA-256 comparison of every file. They use `udisksctl` loop devices, so
they run as a normal user in a desktop session:

```console
$ tests/run_all.sh
```

They cover 512-byte and 4 KiB sectors, 4 KiB–128 KiB clusters, both the
shift path and the no-shift path, fragmented and contiguous files, files
crossing the moved region, filling the new space afterwards, byte-identical
restore, and the wizard on MBR and GPT disk images (including a following
partition) in unattended and interactive mode.

## Contributing

Bug reports and patches are welcome. When reporting a problem, please
include the output of `exfat-grow info DEVICE` (read-only).

## License

Copyright © 2026 Giovanni Andrea Binda

This program is free software; you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation; either version 2 of the License, or (at your
option) any later version. See [LICENSE](LICENSE).

`SPDX-License-Identifier: GPL-2.0-or-later`
