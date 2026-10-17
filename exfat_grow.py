#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Giovanni Andrea Binda <322129496+giobinda@users.noreply.github.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
"""exfat_grow - grow an exFAT filesystem in place to fill its partition.

The FAT has to grow along with the cluster count, but it must fit between
FatOffset and ClusterHeapOffset.  When there is not enough slack there, the
start of the cluster heap is moved forward by k clusters: the data in the
first k clusters is copied into the newly added space at the end of the
volume and every cluster reference (FAT chains, directory entries, root
directory pointer, allocation bitmap) is renumbered.  No other file data
moves.

Usage:
    exfat_grow.py wizard  [PARTITION|DISK|IMAGE] [--yes]   (guided, does everything)
    exfat_grow.py info    DEVICE
    exfat_grow.py grow    DEVICE [--sectors N] [--undo FILE] [--write]
    exfat_grow.py restore DEVICE --undo FILE [--write]

Without --write, grow and restore only print what they would do.
With grow, extend the partition first; the wizard does that for you.
"""

import argparse
import array
import hashlib
import json
import os
import pwd
import re
import stat
import struct
import subprocess
import sys
import time

__version__ = "0.1.0"

EOC = 0xFFFFFFFF
BAD = 0xFFFFFFF7
MAX_CLUSTERS = 0xFFFFFFF5
UNDO_MAGIC = b"EXFGUNDO1\n"
BIT_CHARS = bytes.maketrans(b"\0\1", b"01")

if sys.byteorder != "little":
    sys.exit("this tool assumes a little-endian host")


class FsError(Exception):
    pass


# ---------------------------------------------------------------- checksums

def boot_checksum(region, bps):
    chk = 0
    for i in range(11 * bps):
        if i in (106, 107, 112):
            continue
        chk = ((((chk & 1) << 31) | (chk >> 1)) + region[i]) & 0xFFFFFFFF
    return chk


def set_checksum(buf, start, count):
    chk = 0
    for i in range(count * 32):
        if i in (2, 3):
            continue
        chk = ((((chk & 1) << 15) | (chk >> 1)) + buf[start + i]) & 0xFFFF
    return chk


# ------------------------------------------------------------------- device

class Device:
    """A block device or image file, optionally a byte window into it
    (used for a partition inside a disk image file)."""

    def __init__(self, path, writable, offset=0, length=None):
        st = os.stat(path)
        self.is_blk = stat.S_ISBLK(st.st_mode)
        if writable and self.is_blk:
            check_not_mounted(st.st_rdev)
        flags = os.O_RDWR if writable else os.O_RDONLY
        if writable and self.is_blk:
            flags |= os.O_EXCL  # fails if the device is mounted / in use
        self.fd = os.open(path, flags)
        self.offset = offset
        end = os.lseek(self.fd, 0, os.SEEK_END)
        self.size = end - offset if length is None else length

    def close(self):
        os.close(self.fd)

    def _check(self, off, n):
        if off < 0 or off + n > self.size:
            raise FsError(f"access past end of device ({off}+{n} > {self.size})")

    def read(self, off, n):
        self._check(off, n)
        out = bytearray()
        while len(out) < n:
            b = os.pread(self.fd, n - len(out), self.offset + off + len(out))
            if not b:
                raise FsError(f"short read at offset {off}")
            out += b
        return out

    def write(self, off, data):
        self._check(off, len(data))
        mv = memoryview(data)
        while mv:
            n = os.pwrite(self.fd, mv, self.offset + off)
            off += n
            mv = mv[n:]

    def sync(self):
        os.fsync(self.fd)


def check_not_mounted(rdev):
    with open("/proc/self/mounts") as f:
        for line in f:
            src = line.split()[0]
            try:
                if src.startswith("/") and os.stat(src).st_rdev == rdev:
                    raise FsError(f"device is mounted ({line.split()[1]}); unmount it first")
            except FileNotFoundError:
                pass


# ----------------------------------------------------------------- geometry

class Geometry:
    def __init__(self, region):
        bs = region
        if bs[3:11] != b"EXFAT   ":
            raise FsError("not an exFAT filesystem (bad FileSystemName)")
        if bs[510:512] != b"\x55\xaa":
            raise FsError("bad boot signature")
        (self.partition_offset, self.volume_length, self.fat_offset,
         self.fat_length, self.heap_offset, self.cluster_count,
         self.root_cluster, self.serial, self.revision, self.flags,
         self.bps_shift, self.spc_shift, self.num_fats, _drive,
         self.percent_in_use) = struct.unpack_from("<QQIIIIIIHHBBBBB", bs, 64)
        if not 9 <= self.bps_shift <= 12:
            raise FsError("bad BytesPerSectorShift")
        if self.bps_shift + self.spc_shift > 25:
            raise FsError("bad SectorsPerClusterShift")
        self.bps = 1 << self.bps_shift
        self.spc = 1 << self.spc_shift
        self.cs = self.bps << self.spc_shift

    def heap_byte(self, heap_offset, cluster):
        return (heap_offset << self.bps_shift) + (cluster - 2) * self.cs


def read_boot_region(dev, base_sector, bps):
    region = dev.read(base_sector * bps, 12 * bps)
    return region


def verify_boot_region(region, bps):
    chk = boot_checksum(region, bps)
    stored = array.array("I", bytes(region[11 * bps:12 * bps]))
    return all(x == chk for x in stored)


# ---------------------------------------------------------------- filesystem

class DirEntryRef:
    """A directory entry that holds a FirstCluster field."""
    __slots__ = ("dir", "off", "set_start", "set_count", "flags_off", "kind")

    def __init__(self, d, off, set_start, set_count, flags_off, kind):
        self.dir, self.off = d, off
        self.set_start, self.set_count = set_start, set_count
        self.flags_off, self.kind = flags_off, kind


class Obj:
    """Something that owns clusters: a file, a directory, the bitmap, ..."""
    __slots__ = ("kind", "clusters", "nofat", "ref", "name")

    def __init__(self, kind, clusters, nofat, ref, name):
        self.kind, self.clusters, self.nofat = kind, clusters, nofat
        self.ref, self.name = ref, name


class Directory:
    __slots__ = ("clusters", "buf", "path", "dirty")

    def __init__(self, clusters, buf, path):
        self.clusters, self.buf, self.path, self.dirty = clusters, buf, path, False


class ExFat:
    def __init__(self, dev):
        self.dev = dev
        first = dev.read(0, 512)
        if first[3:11] != b"EXFAT   ":
            raise FsError("not an exFAT filesystem")
        bps = 1 << first[108]
        self.boot = read_boot_region(dev, 0, bps)
        self.g = g = Geometry(self.boot)
        if not verify_boot_region(self.boot, g.bps):
            raise FsError("main boot region checksum mismatch")
        if g.num_fats != 1:
            raise FsError("volumes with 2 FATs (TexFAT) are not supported")
        if g.flags & 0x2:
            raise FsError("volume is marked dirty; run fsck.exfat on it first")
        if g.flags & 0x4:
            raise FsError("volume has MediaFailure flag set")
        n = g.cluster_count + 2
        raw = dev.read(g.fat_offset * g.bps, n * 4)
        self.fat = array.array("I", bytes(raw))
        self.objs = []
        self.dirs = []
        self.bitmap_ref = None
        self.upcase = None

    # -- helpers --
    def cluster_read(self, c, count=1):
        g = self.g
        return self.dev.read(g.heap_byte(g.heap_offset, c), count * g.cs)

    def valid(self, c):
        return 2 <= c < self.g.cluster_count + 2

    def chain(self, first, nofat, length, what):
        g = self.g
        if first == 0:
            return []
        if not self.valid(first):
            raise FsError(f"{what}: first cluster {first} out of range")
        if nofat:
            n = -(-length // g.cs)
            if n == 0:
                return []
            if first + n > g.cluster_count + 2:
                raise FsError(f"{what}: contiguous run past end of heap")
            return range(first, first + n)
        out = []
        c = first
        limit = g.cluster_count
        while True:
            out.append(c)
            if len(out) > limit:
                raise FsError(f"{what}: FAT chain loop")
            nxt = self.fat[c]
            if nxt >= 0xFFFFFFF8:
                break
            if not self.valid(nxt):
                raise FsError(f"{what}: FAT chain points to invalid cluster {nxt:#x}")
            c = nxt
        return out

    def read_dir(self, clusters, path):
        buf = bytearray()
        # coalesce contiguous runs into single reads
        i = 0
        cl = list(clusters)
        while i < len(cl):
            j = i
            while j + 1 < len(cl) and cl[j + 1] == cl[j] + 1:
                j += 1
            buf += self.cluster_read(cl[i], j - i + 1)
            i = j + 1
        d = Directory(cl, buf, path)
        self.dirs.append(d)
        return d

    # -- scan --
    def scan(self):
        g = self.g
        root_clusters = self.chain(g.root_cluster, False, 0, "root directory")
        root = self.read_dir(root_clusters, "/")
        self.objs.append(Obj("rootdir", root_clusters, False, None, "/"))
        stack = [root]
        while stack:
            d = stack.pop()
            stack.extend(self.parse_dir(d))
        if self.bitmap_ref is None:
            raise FsError("no allocation bitmap entry in root directory")
        if self.upcase is None:
            raise FsError("no up-case table entry in root directory")

    def parse_dir(self, d):
        buf = d.buf
        n = len(buf) // 32
        subdirs = []
        i = 0
        is_root = d.path == "/"
        while i < n:
            off = i * 32
            t = buf[off]
            if t == 0:
                break
            if not t & 0x80:
                i += 1
                continue
            if t == 0x81:
                if not is_root:
                    raise FsError("bitmap entry outside root directory")
                if buf[off + 1] & 1:
                    raise FsError("second allocation bitmap present (TexFAT)")
                first, length = struct.unpack_from("<IQ", buf, off + 20)
                ref = DirEntryRef(d, off, None, 0, None, "bitmap")
                cl = self.chain(first, True, length, "allocation bitmap")
                self.bitmap_ref = ref
                self.objs.append(Obj("bitmap", cl, True, ref, "<bitmap>"))
                i += 1
            elif t == 0x82:
                first, length = struct.unpack_from("<IQ", buf, off + 20)
                ref = DirEntryRef(d, off, None, 0, None, "upcase")
                cl = self.chain(first, False, length, "up-case table")
                self.upcase = Obj("upcase", cl, False, ref, "<upcase>")
                self.objs.append(self.upcase)
                i += 1
            elif t == 0x83:
                i += 1
            elif t & 0x40:
                # stray secondary entry outside a set: ignore like the kernel
                i += 1
            elif t == 0x85 or t & 0x20:
                # file entry set, or benign primary with secondaries
                sc = buf[off + 1]
                if i + sc >= n:
                    raise FsError(f"{d.path}: entry set runs past end of directory")
                stored = struct.unpack_from("<H", buf, off + 2)[0]
                if set_checksum(buf, off, sc + 1) != stored:
                    raise FsError(f"{d.path}: entry set checksum mismatch at entry {i}")
                name = ""
                is_dir = False
                if t == 0x85:
                    is_dir = bool(struct.unpack_from("<H", buf, off + 4)[0] & 0x10)
                    for j in range(2, sc + 1):
                        e = off + j * 32
                        if buf[e] == 0xC1:
                            name += bytes(buf[e + 2:e + 32]).decode("utf-16-le", "replace")
                    name = name.split("\0")[0]
                else:
                    # benign primary using the generic primary template
                    pflags = struct.unpack_from("<H", buf, off + 4)[0]
                    if pflags & 1:
                        first, length = struct.unpack_from("<IQ", buf, off + 20)
                        if first:
                            ref = DirEntryRef(d, off, off, sc + 1, off + 4, "primary")
                            cl = self.chain(first, bool(pflags & 2), length, f"{d.path}: entry {t:#x}")
                            self.objs.append(Obj("benign", cl, bool(pflags & 2), ref, f"{d.path}<{t:#x}>"))
                path = d.path.rstrip("/") + "/" + name
                for j in range(1, sc + 1):
                    e = off + j * 32
                    st = buf[e]
                    if not st & 0x80 or not st & 0x40:
                        raise FsError(f"{path}: malformed entry set")
                    if t == 0x85 and j == 1 and st != 0xC0:
                        raise FsError(f"{path}: file entry not followed by stream extension")
                    if not st & 0x20 and st not in (0xC0, 0xC1):
                        raise FsError(f"{path}: unknown critical secondary entry {st:#x}")
                    if st == 0xC1:
                        continue
                    sflags = buf[e + 1]
                    if not sflags & 1:
                        continue
                    first, length = struct.unpack_from("<IQ", buf, e + 20)
                    if first == 0:
                        continue
                    nofat = bool(sflags & 2)
                    ref = DirEntryRef(d, e, off, sc + 1, e + 1, "secondary")
                    cl = self.chain(first, nofat, length, path)
                    if st == 0xC0 and is_dir:
                        self.objs.append(Obj("dir", cl, nofat, ref, path))
                        subdirs.append(self.read_dir(cl, path))
                    else:
                        self.objs.append(Obj("file", cl, nofat, ref, path))
                i += sc + 1
            else:
                raise FsError(f"{d.path}: unknown critical primary entry {t:#x}")
        return subdirs

    def load_bitmap(self):
        g = self.g
        bm = self.objs[[o.kind for o in self.objs].index("bitmap")]
        first = bm.clusters[0]
        nbytes = -(-g.cluster_count // 8)
        raw = self.dev.read(g.heap_byte(g.heap_offset, first), nbytes)
        self.bitmap_bytes = raw
        # one byte per cluster, indexed by cluster number
        table = [bytes((b >> k) & 1 for k in range(8)) for b in range(256)]
        exp = bytearray(b"\0\0") + bytearray(b"".join(table[b] for b in raw))
        del exp[g.cluster_count + 2:]
        self.alloc = exp

    def check_ownership(self):
        """Every referenced cluster must be marked allocated and owned once."""
        g = self.g
        owner = bytearray(g.cluster_count + 2)
        for o in self.objs:
            cl = o.clusters
            if isinstance(cl, range):
                s, e = cl.start, cl.stop
                if owner.find(1, s, e) != -1:
                    raise FsError(f"{o.name}: cross-linked clusters")
                if self.alloc.find(0, s, e) != -1:
                    raise FsError(f"{o.name}: uses clusters not marked in allocation bitmap")
                owner[s:e] = b"\1" * (e - s)
            else:
                for c in cl:
                    if owner[c]:
                        raise FsError(f"{o.name}: cross-linked cluster {c}")
                    if not self.alloc[c]:
                        raise FsError(f"{o.name}: cluster {c} not marked in allocation bitmap")
                    owner[c] = 1
        self.owner = owner
        used = self.alloc.count(1)
        owned = owner.count(1)
        return used, owned


# --------------------------------------------------------------------- plan

class Plan:
    pass


def compute_layout(g, new_volume_length):
    """Find the smallest heap shift k (in clusters) that leaves room for the FAT."""
    k = 0
    while True:
        heap = g.heap_offset + k * g.spc
        if heap >= new_volume_length:
            raise FsError("volume too small")
        cc = min((new_volume_length - heap) >> g.spc_shift, MAX_CLUSTERS)
        need = -(-(cc + 2) * 4 // g.bps)
        if g.fat_offset + need <= heap:
            # round FAT length up to a whole cluster when it fits
            fat_len = min(-(-need // g.spc) * g.spc, heap - g.fat_offset)
            return k, heap, cc, fat_len
        # jump close to the answer instead of stepping one cluster at a time
        short = g.fat_offset + need - heap
        k += max(1, short // g.spc)


def make_plan(fs, new_volume_length):
    g = fs.g
    p = Plan()
    p.k, p.heap, p.cc, p.fat_len = compute_layout(g, new_volume_length)
    p.volume_length = new_volume_length
    k, old_cc = p.k, g.cluster_count
    if p.cc <= old_cc - k:
        raise FsError(f"nothing to grow: new cluster count {p.cc} <= current {old_cc}"
                      + (f" - shift {k}" if k else ""))
    p.dbase = old_cc + 2 - k          # first new-numbering cluster of the added space
    p.bitmap_bytes = -(-p.cc // 8)
    p.bitmap_clusters = -(-p.bitmap_bytes // g.cs)
    p.bitmap_first = p.dbase + k
    nxt = p.bitmap_first + p.bitmap_clusters

    lo = 2 + k
    p.M = lambda c: c - k if c >= lo else p.dbase + c - 2

    # up-case table must stay contiguous (the Linux driver reads it linearly)
    up = list(fs.upcase.clusters)
    p.upcase_reloc = bool(k) and any(c < lo for c in up) and any(c >= lo for c in up)
    if p.upcase_reloc:
        p.upcase_first = nxt
        nxt += len(up)
    if nxt > p.cc + 2:
        raise FsError("not enough new space for relocated metadata")

    # objects without a FAT chain that straddle the moved region become chained
    p.convert = []
    if k:
        for o in fs.objs:
            if o.nofat and o.kind not in ("bitmap",) and o.clusters:
                s, e = o.clusters[0], o.clusters[-1]
                if s < lo <= e:
                    p.convert.append(o)
    bad_moved = [c for c in range(2, min(lo, old_cc + 2)) if fs.fat[c] == BAD]
    if bad_moved:
        raise FsError("bad clusters in the region that would become FAT space")
    return p


def build(fs, p):
    """Compute every write. Returns list of (byte offset, data, label)."""
    g = fs.g
    k, old_cc = p.k, g.cluster_count
    lo = 2 + k
    M = p.M
    alloc = bytearray(fs.alloc)
    bitmap_obj = next(o for o in fs.objs if o.kind == "bitmap")
    for c in bitmap_obj.clusters:
        alloc[c] = 0
    up = list(fs.upcase.clusters)
    if p.upcase_reloc:
        for c in up:
            alloc[c] = 0
    writes = []

    # -- 1. copy data out of the first k clusters into the added space
    evac = [c for c in range(2, min(lo, old_cc + 2)) if alloc[c]]
    i = 0
    while i < len(evac):
        j = i
        while j + 1 < len(evac) and evac[j + 1] == evac[j] + 1 and j - i < 255:
            j += 1
        data = fs.cluster_read(evac[i], j - i + 1)
        writes.append((g.heap_byte(p.heap, M(evac[i])), data, "move clusters"))
        i = j + 1
    if p.upcase_reloc:
        data = b"".join(fs.cluster_read(c) for c in up)
        writes.append((g.heap_byte(p.heap, p.upcase_first), data, "relocate up-case table"))

    # -- 2. new FAT
    fat_entries = p.fat_len * g.bps // 4
    nf = array.array("I", bytes(fat_entries * 4))
    nf[0], nf[1] = 0xFFFFFFF8, 0xFFFFFFFF
    of = fs.fat
    end = old_cc + 2
    for c in range(2, end):
        if not alloc[c]:
            continue
        v = of[c]
        if v == 0:
            continue
        if 2 <= v < end:
            v = v - k if v >= lo else p.dbase + v - 2
        elif v == BAD:
            pass
        elif v >= 0xFFFFFFF8:
            v = EOC
        else:
            v = 0
        nf[c - k if c >= lo else p.dbase + c - 2] = v

    def chain_into(clusters):
        for a, b in zip(clusters, clusters[1:]):
            nf[a] = b
        nf[clusters[-1]] = EOC

    bm_new = list(range(p.bitmap_first, p.bitmap_first + p.bitmap_clusters))
    chain_into(bm_new)
    if p.upcase_reloc:
        up_new = list(range(p.upcase_first, p.upcase_first + len(up)))
        chain_into(up_new)
    for o in p.convert:
        chain_into([M(c) for c in o.clusters])
    writes.append((g.fat_offset * g.bps, nf.tobytes(), "new FAT"))

    # -- 3. new allocation bitmap
    # one byte per cluster: non-moved clusters shift down by k, moved ones go to dbase
    nb = bytearray(p.cc + 2)
    nb[2:end - k] = alloc[lo:end]
    nb[p.dbase:p.dbase + (lo - 2)] = alloc[2:lo]
    for c in bm_new:
        nb[c] = 1
    if p.upcase_reloc:
        for c in up_new:
            nb[c] = 1
    bits = bytes(nb[2:])
    used = bits.count(1)
    packed = int(bits.translate(BIT_CHARS)[::-1], 2).to_bytes(p.bitmap_bytes, "little")
    bmdata = bytes(packed) + bytes(p.bitmap_clusters * g.cs - len(packed))
    writes.append((g.heap_byte(p.heap, p.bitmap_first), bmdata, "new allocation bitmap"))

    # -- 4. directory entries
    touched_sets = set()
    for o in fs.objs:
        r = o.ref
        if r is None:
            continue
        buf = r.dir.buf
        if o.kind == "bitmap":
            struct.pack_into("<IQ", buf, r.off + 20, p.bitmap_first, p.bitmap_bytes)
        elif o.kind == "upcase" and p.upcase_reloc:
            struct.pack_into("<I", buf, r.off + 20, p.upcase_first)
        else:
            old = struct.unpack_from("<I", buf, r.off + 20)[0]
            struct.pack_into("<I", buf, r.off + 20, M(old))
            if o in p.convert:
                if r.kind == "primary":
                    fl = struct.unpack_from("<H", buf, r.flags_off)[0] & ~2
                    struct.pack_into("<H", buf, r.flags_off, fl)
                else:
                    buf[r.flags_off] &= ~2 & 0xFF
        r.dir.dirty = True
        if r.set_start is not None:
            touched_sets.add((id(r.dir), r.set_start, r.set_count))
    by_id = {id(d): d for d in fs.dirs}
    for did, start, count in touched_sets:
        buf = by_id[did].buf
        struct.pack_into("<H", buf, start + 2, set_checksum(buf, start, count))

    for d in fs.dirs:
        if not d.dirty:
            continue
        for idx, c in enumerate(d.clusters):
            writes.append((g.heap_byte(p.heap, M(c)),
                           bytes(d.buf[idx * g.cs:(idx + 1) * g.cs]),
                           f"directory {d.path}"))

    # -- 5. boot regions
    region = bytearray(fs.boot)
    flags = g.flags & ~0x3  # clean, first FAT active
    pct = min(100, used * 100 // p.cc) if p.cc else 0
    struct.pack_into("<QQIIIII", region, 64, g.partition_offset, p.volume_length,
                     g.fat_offset, p.fat_len, p.heap, p.cc, M(g.root_cluster))
    struct.pack_into("<H", region, 106, flags)
    region[112] = pct
    chk = boot_checksum(region, g.bps)
    region[11 * g.bps:12 * g.bps] = struct.pack("<I", chk) * (g.bps // 4)
    p.final_boot = (0, bytes(region), "main boot region")
    p.backup_boot = (12 * g.bps, bytes(region), "backup boot region")
    p.used_after = used
    return writes


# --------------------------------------------------------------------- undo

def save_undo(dev, path, regions, info):
    meta = dict(info, regions=[[off, len(data)] for off, data, _ in regions])
    h = hashlib.sha256()
    with open(path, "xb") as f:
        mj = json.dumps(meta).encode()
        f.write(UNDO_MAGIC + struct.pack("<I", len(mj)) + mj)
        h.update(mj)
        for off, data, _ in regions:
            orig = dev.read(off, len(data))
            h.update(orig)
            f.write(orig)
        f.write(h.digest())
        f.flush()
        os.fsync(f.fileno())


def load_undo(path):
    with open(path, "rb") as f:
        if f.read(len(UNDO_MAGIC)) != UNDO_MAGIC:
            raise FsError("not an exfat_grow undo file")
        n = struct.unpack("<I", f.read(4))[0]
        mj = f.read(n)
        meta = json.loads(mj)
        h = hashlib.sha256(mj)
        regions = []
        for off, ln in meta["regions"]:
            d = f.read(ln)
            if len(d) != ln:
                raise FsError("undo file truncated")
            h.update(d)
            regions.append((off, d))
        if f.read(32) != h.digest():
            raise FsError("undo file checksum mismatch")
    return meta, regions


# --------------------------------------------------------------------- main

def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


def describe(fs):
    g = fs.g
    print(f"  sector size        {g.bps}")
    print(f"  cluster size       {human(g.cs)}")
    print(f"  volume length      {g.volume_length} sectors ({human(g.volume_length * g.bps)})")
    print(f"  FAT                offset {g.fat_offset}, length {g.fat_length} sectors")
    print(f"  cluster heap       offset {g.heap_offset}, {g.cluster_count} clusters")
    print(f"  root dir cluster   {g.root_cluster}")
    print(f"  device size        {fs.dev.size // g.bps} sectors ({human(fs.dev.size)})")


def open_and_scan(path, writable, offset=0, length=None):
    dev = Device(path, writable, offset, length)
    fs = ExFat(dev)
    fs.scan()
    fs.load_bitmap()
    used, owned = fs.check_ownership()
    return fs, used, owned


def target_length(fs, args):
    g = fs.g
    dev_sectors = fs.dev.size // g.bps
    n = args.sectors if args.sectors else dev_sectors
    if n > dev_sectors:
        raise FsError(f"--sectors {n} exceeds device size {dev_sectors}")
    return n


def cmd_info(args):
    fs, used, owned = open_and_scan(args.device, False)
    print(f"{args.device}: exFAT, serial {fs.g.serial:08X}")
    describe(fs)
    print(f"  objects scanned    {len(fs.objs)} ({len(fs.dirs)} directories)")
    print(f"  clusters in use    {used} (referenced: {owned}, leaked: {used - owned})")
    try:
        p = make_plan(fs, target_length(fs, args))
        report_plan(fs, p)
    except FsError as e:
        print(f"  grow: {e}")


def report_plan(fs, p):
    g = fs.g
    print("Plan:")
    print(f"  new volume length  {p.volume_length} sectors ({human(p.volume_length * g.bps)})")
    print(f"  new cluster count  {p.cc} (+{p.cc - g.cluster_count})")
    print(f"  new FAT length     {p.fat_len} sectors (was {g.fat_length})")
    if p.k:
        moved = sum(1 for c in range(2, min(2 + p.k, g.cluster_count + 2)) if fs.alloc[c])
        print(f"  heap shift         {p.k} clusters; heap offset {g.heap_offset} -> {p.heap}")
        print(f"  clusters moved     {moved} ({human(moved * g.cs)})")
    else:
        print("  heap shift         none needed (FAT fits)")
    print(f"  new bitmap         {p.bitmap_clusters} clusters at {p.bitmap_first}")
    if p.upcase_reloc:
        print(f"  up-case table      relocated to {p.upcase_first}")
    for o in p.convert:
        print(f"  converted to FAT chain: {o.name}")


def prepare_grow(fs, sectors):
    """Plan and build every write in memory; nothing touches the device."""
    p = make_plan(fs, sectors)
    writes = build(fs, p)
    allw = writes + [p.final_boot, p.backup_boot]
    vol_end = p.volume_length * fs.g.bps
    for off, d, label in allw:
        if off + len(d) > vol_end:
            raise FsError(f"internal error: write '{label}' past end of volume")
    return p, writes, allw


def apply_grow(fs, p, writes, allw, undo, meta):
    g = fs.g
    print(f"Saving undo data to {undo} ...")
    dirty = bytearray(fs.boot[:g.bps])
    struct.pack_into("<H", dirty, 106, g.flags | 0x2)
    save_undo(fs.dev, undo, [(0, bytes(dirty), "")] + allw,
              dict(meta, serial=g.serial, device_size=fs.dev.size, offset=fs.dev.offset))
    print("Marking volume dirty ...")
    fs.dev.write(0, dirty)
    fs.dev.sync()
    print(f"Writing {human(sum(len(d) for _, d, _ in writes))} of relocated data and metadata ...")
    for off, d, label in writes:
        fs.dev.write(off, d)
    fs.dev.sync()
    print("Writing new boot regions ...")
    fs.dev.write(*p.backup_boot[:2])
    fs.dev.sync()
    fs.dev.write(*p.final_boot[:2])
    fs.dev.sync()


def default_undo_name(serial):
    return f"exfat-grow-undo-{serial:08X}-{time.strftime('%Y%m%d-%H%M%S')}.bin"


def cmd_grow(args):
    fs, used, owned = open_and_scan(args.device, args.write)
    g = fs.g
    print(f"{args.device}: exFAT, serial {g.serial:08X}")
    describe(fs)
    p, writes, allw = prepare_grow(fs, target_length(fs, args))
    report_plan(fs, p)
    print(f"  total writes       {len(allw)} ({human(sum(len(d) for _, d, _ in allw))})")
    if not args.write:
        print("\nDry run: nothing written. Re-run with --write to apply.")
        return
    undo = args.undo or default_undo_name(g.serial)
    print()
    apply_grow(fs, p, writes, allw, undo, {"device": args.device})
    print("Done. Verify with: fsck.exfat -n " + args.device)
    print(f"Keep {undo} until you have checked the result; "
          f"`exfat_grow.py restore {args.device} --undo {undo} --write` reverts it.")


def restore_regions(dev, regions):
    # every region holds pre-grow content; write the boot regions last so an
    # interrupted restore leaves the volume marked dirty
    first, rest, boots = regions[0], regions[1:-2], regions[-2:]
    for off, d in rest + boots[::-1] + [first]:
        dev.write(off, d)
    dev.sync()


def cmd_restore(args):
    meta, regions = load_undo(args.undo)
    offset = meta.get("offset", 0)
    dev = Device(args.device, args.write, offset, meta["device_size"] if offset else None)
    if dev.size != meta["device_size"]:
        print(f"warning: device size {dev.size} differs from recorded {meta['device_size']}")
    print(f"Undo file for serial {meta['serial']:08X}, {len(regions)} regions, "
          f"{human(sum(len(d) for _, d in regions))}")
    if not args.write:
        print("Dry run: nothing written. Re-run with --write to apply.")
        return
    restore_regions(dev, regions)
    print("Restored. Verify with: fsck.exfat -n " + args.device)
    if meta.get("partition_backup"):
        print("To also restore the old partition table: "
              f"sfdisk {meta['disk']} < {meta['partition_backup']}")


# ------------------------------------------------------------------- wizard

def run(cmd, input=None, check=True):
    try:
        r = subprocess.run(cmd, input=input, text=True, capture_output=True)
    except FileNotFoundError:
        raise FsError(f"required program not found: {cmd[0]}")
    if check and r.returncode != 0:
        raise FsError(f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()}")
    return r


def trailing_number(s):
    m = re.search(r"(\d+)$", s)
    return int(m.group(1)) if m else None


class Target:
    """What the wizard works on.

    kind "blockpart": a partition block device (/dev/sda1) on disk /dev/sda.
    kind "imagepart": partition N inside a disk image file (byte window).
    kind "image":     a bare filesystem image file (no partition table).
    """

    def __init__(self, kind, fs_path, disk=None, partnum=None):
        self.kind, self.fs_path, self.disk, self.partnum = kind, fs_path, disk, partnum

    def label(self):
        if self.kind == "imagepart":
            return f"{self.disk} partition {self.partnum}"
        return self.fs_path

    def window(self):
        """(offset, length) in bytes of the filesystem inside fs_path."""
        if self.kind != "imagepart":
            return 0, None
        t, p = partition_entry(self.disk, self.partnum)
        ss = t.get("sectorsize", 512)
        return p["start"] * ss, p["size"] * ss


def partition_table(disk):
    r = run(["sfdisk", "-J", disk], check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout)["partitiontable"]


def partition_entry(disk, partnum):
    t = partition_table(disk)
    if t is None:
        raise FsError(f"cannot read partition table of {disk}")
    for p in t.get("partitions", []):
        if trailing_number(p["node"]) == partnum:
            return t, p
    raise FsError(f"partition {partnum} not found on {disk}")


def file_size(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def is_exfat_at(path, offset):
    try:
        with open(path, "rb") as f:
            f.seek(offset + 3)
            return f.read(8) == b"EXFAT   "
    except OSError:
        return False


class Wizard:
    def __init__(self, args):
        self.args = args
        self.n = 0

    # -- interaction --
    def step(self, title):
        self.n += 1
        print(f"\n\033[1m[{self.n}] {title}\033[0m" if sys.stdout.isatty() else f"\n[{self.n}] {title}")

    def ask(self, q, default=True):
        if self.args.yes:
            print(f"  {q} -> yes (--yes)")
            return True
        if not sys.stdin.isatty():
            raise FsError("not running in a terminal; use --yes for unattended mode")
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            a = input("  " + q + suffix).strip().lower()
            if not a:
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False

    def confirm(self, q, default=True):
        if not self.ask(q, default):
            raise FsError("aborted; nothing was changed in this step")

    def choose(self, what, options):
        """options: list of (value, description)."""
        if len(options) == 1:
            value, desc = options[0]
            self.confirm(f"Use {desc}?")
            return value
        if self.args.yes:
            raise FsError(f"several {what}s found; name one explicitly with --yes")
        for i, (_, desc) in enumerate(options, 1):
            print(f"    {i}) {desc}")
        while True:
            a = input(f"  Which {what}? [1-{len(options)}] ").strip()
            if a.isdigit() and 1 <= int(a) <= len(options):
                return options[int(a) - 1][0]

    # -- steps --
    def resolve(self):
        self.step("Select the exFAT filesystem")
        arg = self.args.target
        if arg is None:
            return self.pick_block(None)
        st = os.stat(arg)
        if stat.S_ISBLK(st.st_mode):
            name = os.path.basename(os.path.realpath(arg))
            sysdir = f"/sys/class/block/{name}"
            if os.path.exists(f"{sysdir}/partition"):
                with open(f"{sysdir}/partition") as f:
                    partnum = int(f.read())
                disk = "/dev/" + os.path.basename(os.path.dirname(os.path.realpath(sysdir)))
                print(f"  {arg} is partition {partnum} of {disk}")
                return Target("blockpart", arg, disk, partnum)
            return self.pick_block(arg)
        if not stat.S_ISREG(st.st_mode):
            raise FsError(f"{arg} is neither a block device nor a file")
        t = partition_table(arg)
        if t and t.get("partitions"):
            ss = t.get("sectorsize", 512)
            opts = []
            for p in t["partitions"]:
                num = trailing_number(p["node"])
                if self.args.partition and num != self.args.partition:
                    continue
                if is_exfat_at(arg, p["start"] * ss):
                    opts.append((num, f"partition {num} of image {arg} "
                                      f"({human(p['size'] * ss)}, exFAT)"))
            if not opts:
                raise FsError(f"no exFAT partition found in {arg}")
            num = self.choose("partition", opts)
            return Target("imagepart", arg, arg, num)
        if not is_exfat_at(arg, 0):
            raise FsError(f"{arg} contains no exFAT filesystem")
        print(f"  {arg} is a bare filesystem image ({human(st.st_size)})")
        return Target("image", arg)

    def pick_block(self, disk):
        cmd = ["lsblk", "-J", "-b", "-o", "PATH,SIZE,FSTYPE,LABEL,MOUNTPOINTS,TYPE,PKNAME"]
        if disk:
            cmd.append(disk)
        devs = []

        def walk(nodes):
            for d in nodes:
                if d.get("fstype") == "exfat" and d.get("type") == "part":
                    devs.append(d)
                walk(d.get("children", []))
        walk(json.loads(run(cmd).stdout)["blockdevices"])
        if not devs:
            raise FsError("no exFAT partitions found" + (f" on {disk}" if disk else ""))
        opts = []
        for d in devs:
            mps = [m for m in d.get("mountpoints") or [] if m]
            desc = f"{d['path']} ({human(int(d['size']))}"
            desc += f", label '{d['label']}'" if d.get("label") else ""
            desc += f", mounted on {', '.join(mps)})" if mps else ")"
            opts.append((d["path"], desc))
        path = self.choose("partition", opts)
        self.args.target = path
        return self.resolve_quiet(path)

    def resolve_quiet(self, path):
        name = os.path.basename(os.path.realpath(path))
        sysdir = f"/sys/class/block/{name}"
        with open(f"{sysdir}/partition") as f:
            partnum = int(f.read())
        disk = "/dev/" + os.path.basename(os.path.dirname(os.path.realpath(sysdir)))
        return Target("blockpart", path, disk, partnum)

    def unmount(self, t):
        if t.kind != "blockpart":
            return
        r = run(["findmnt", "-rn", "-S", t.fs_path, "-o", "TARGET"], check=False)
        mps = [m for m in r.stdout.split("\n") if m]
        if not mps:
            return
        self.step("Unmount")
        print(f"  {t.fs_path} is mounted on: {', '.join(mps)}")
        self.confirm("Unmount it now?")
        for m in mps:
            run(["umount", m])
        print("  unmounted")

    def fsck(self, t, phase):
        if t.kind == "imagepart":
            print("  (fsck.exfat cannot check a partition inside an image file; "
                  "relying on this tool's own consistency checks)")
            return True
        r = run(["fsck.exfat", "-n", t.fs_path], check=False)
        out = (r.stdout + r.stderr).strip().split("\n")
        for line in out[-3:]:
            print("  " + line)
        if r.returncode == 0:
            return True
        if phase == "after":
            return False
        print("  fsck.exfat reported problems. The filesystem must be clean before resizing.")
        if self.args.yes:
            raise FsError("filesystem not clean; run `fsck.exfat` yourself, then retry")
        self.confirm("Let fsck.exfat repair it now (fsck.exfat -y)?", default=False)
        r = run(["fsck.exfat", "-y", t.fs_path], check=False)
        print("  " + (r.stdout + r.stderr).strip().replace("\n", "\n  "))
        r = run(["fsck.exfat", "-n", t.fs_path], check=False)
        if r.returncode != 0:
            raise FsError("filesystem still not clean after repair")
        print("  filesystem is clean now")
        return True

    def grow_partition(self, t, backup_dir):
        """Returns path of the partition table backup, or None if unchanged."""
        self.step("Grow the partition")
        if t.kind == "image":
            print("  bare image: no partition table; the filesystem will fill the file")
            return None
        tab, p = partition_entry(t.disk, t.partnum)
        label = tab.get("label")
        ss = tab.get("sectorsize", 512)
        if label not in ("dos", "gpt"):
            raise FsError(f"unsupported partition table type {label!r}")
        if label == "dos" and t.partnum > 4:
            raise FsError("logical (extended) partitions are not supported")
        disk_sectors = file_size(t.disk) // ss
        last = tab.get("lastlba", disk_sectors - 34) if label == "gpt" else disk_sectors - 1
        start, size = p["start"], p["size"]
        nexts = [q["start"] for q in tab["partitions"] if q["start"] > start]
        max_size = min([last] + [s - 1 for s in nexts]) - start + 1
        print(f"  table {label}, partition {t.partnum}: start {start}, "
              f"size {size} sectors ({human(size * ss)})")
        if max_size - size < (1 << 20) // ss:
            print(f"  partition already uses all available space "
                  f"(max {human(max_size * ss)}); not changing it")
            return None
        print(f"  can grow to {max_size} sectors ({human(max_size * ss)}), "
              f"+{human((max_size - size) * ss)}; the start sector stays {start}")
        name = os.path.basename(t.disk)
        backup = os.path.join(backup_dir, f"{name}-partition-table-{time.strftime('%Y%m%d-%H%M%S')}.sfdisk")
        self.confirm(f"Resize partition {t.partnum} to {human(max_size * ss)}? "
                     f"(old table is saved to {backup})")
        with open(backup, "x") as f:
            f.write(run(["sfdisk", "-d", t.disk]).stdout)
        self.chown(backup)
        run(["sfdisk", "-q", "--no-reread", "--wipe", "never", "--wipe-partitions", "never",
             "-N", str(t.partnum), t.disk], input=f"{start},{max_size}\n")
        _, p = partition_entry(t.disk, t.partnum)
        if p["start"] != start or p["size"] != max_size:
            raise FsError(f"partition table not as expected after sfdisk; "
                          f"restore with: sfdisk {t.disk} < {backup}")
        if t.kind == "blockpart":
            self.tell_kernel(t, max_size * ss)
        print(f"  partition {t.partnum} is now {human(max_size * ss)}")
        return backup

    def tell_kernel(self, t, want_bytes):
        name = os.path.basename(os.path.realpath(t.fs_path))

        def kernel_size():
            run(["udevadm", "settle"], check=False)
            with open(f"/sys/class/block/{name}/size") as f:
                return int(f.read()) * 512

        for cmd in (["partx", "-u", "--nr", str(t.partnum), t.disk],
                    ["blockdev", "--rereadpt", t.disk]):
            if kernel_size() == want_bytes:
                return
            run(cmd, check=False)
        if kernel_size() != want_bytes:
            raise FsError("the kernel did not pick up the new partition size; reboot "
                          "(or replug the disk) and run the wizard again; it will skip "
                          "the partition step")

    def chown(self, path):
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid and os.geteuid() == 0:
            os.chown(path, int(uid), int(gid))

    def backup_dir(self, t):
        d = self.args.backup_dir
        if not d:
            user = os.environ.get("SUDO_USER")
            d = pwd.getpwnam(user).pw_dir if user else os.getcwd()
        d = os.path.abspath(d)
        if t.kind == "blockpart":
            src = run(["findmnt", "-n", "-o", "SOURCE", "-T", d]).stdout.strip()
            src = src.split("[")[0]
            disk_name = os.path.basename(t.disk)
            if src.startswith("/dev/"):
                pk = run(["lsblk", "-no", "PKNAME", src], check=False).stdout.strip()
                if os.path.basename(src) == disk_name or pk == disk_name:
                    raise FsError(f"backup directory {d} is on the disk being modified; "
                                  "pass --backup-dir on another disk")
        return d

    def run_all(self):
        t = self.resolve()
        bdir = self.backup_dir(t)
        print(f"  backups (undo data, partition table) go to: {bdir}")
        self.unmount(t)

        self.step("Check the filesystem")
        self.fsck(t, "before")
        off, ln = t.window()
        fs, used, owned = open_and_scan(t.fs_path, False, off, ln)
        g = fs.g
        print(f"  exFAT serial {g.serial:08X}, {human(g.cs)} clusters, "
              f"{human(g.cluster_count * g.cs)} heap, {human(used * g.cs)} in use, "
              f"{len(fs.objs)} objects in {len(fs.dirs)} directories: consistent")
        fs.dev.close()

        ptbackup = self.grow_partition(t, bdir)
        self.unmount(t)  # a desktop may have auto-mounted it after the table change

        self.step("Plan the filesystem resize")
        off, ln = t.window()
        fs, used, owned = open_and_scan(t.fs_path, True, off, ln)
        g = fs.g
        target = fs.dev.size // g.bps
        if target - g.volume_length < (1 << 20) // g.bps:
            print(f"  filesystem already fills its partition ({human(g.volume_length * g.bps)}); "
                  "nothing to do")
            return
        p, writes, allw = prepare_grow(fs, target)
        report_plan(fs, p)
        print(f"  {human(g.volume_length * g.bps)} -> {human(p.volume_length * g.bps)}, "
              f"{human(sum(len(d) for _, d, _ in allw))} to write")

        self.step("Resize the filesystem")
        undo = os.path.join(bdir, default_undo_name(g.serial))
        self.confirm(f"Apply? (undo data is saved to {undo} first)")
        meta = {"device": t.fs_path, "disk": t.disk, "partition_backup": ptbackup}
        apply_grow(fs, p, writes, allw, undo, meta)
        self.chown(undo)
        fs.dev.close()

        self.step("Verify")
        ok = self.fsck(t, "after")
        if ok:
            off, ln = t.window()
            fs, used, owned = open_and_scan(t.fs_path, False, off, ln)
            print(f"  re-scan: {fs.g.cluster_count} clusters "
                  f"({human(fs.g.cluster_count * fs.g.cs)}), all metadata consistent")
            fs.dev.close()
            print(f"\nDone: {t.label()} is now {human(p.volume_length * g.bps)}.")
            print(f"Once you have checked your files, you can delete {undo}")
            if ptbackup:
                print(f"and {ptbackup}.")
            return
        print("\n  VERIFICATION FAILED.")
        restore_cmd = f"{sys.argv[0]} restore {t.fs_path} --undo {undo} --write"
        if self.ask("Restore the previous state from the undo data now?"):
            meta, regions = load_undo(undo)
            if t.kind == "imagepart":
                dev = Device(t.fs_path, True, meta["offset"], meta["device_size"])
            else:
                dev = Device(t.fs_path, True)
            restore_regions(dev, regions)
            dev.close()
            if ptbackup:
                with open(ptbackup) as f:
                    run(["sfdisk", "-q", "--no-reread", t.disk], input=f.read())
                if t.kind == "blockpart":
                    tab, part = partition_entry(t.disk, t.partnum)
                    self.tell_kernel(t, part["size"] * tab.get("sectorsize", 512))
            print("  restored previous filesystem and partition table")
        else:
            print(f"  restore later with: {restore_cmd}")
            if ptbackup:
                print(f"  and: sfdisk {t.disk} < {ptbackup}")
        raise FsError("verification failed")


def cmd_wizard(args):
    needs_root = args.target is None or stat.S_ISBLK(os.stat(args.target).st_mode)
    if needs_root and os.geteuid() != 0:
        print("Root privileges are needed to access the disk; re-running with sudo ...")
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    Wizard(args).run_all()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("info", help="show geometry and what grow would do")
    a.add_argument("device")
    a.add_argument("--sectors", type=int, help="target volume length in sectors (default: whole device)")
    a = sub.add_parser("grow", help="grow the filesystem")
    a.add_argument("device")
    a.add_argument("--sectors", type=int, help="target volume length in sectors (default: whole device)")
    a.add_argument("--undo", help="where to save undo data (default: ./exfat-grow-undo-*.bin)")
    a.add_argument("--write", action="store_true", help="actually modify the device")
    a = sub.add_parser("wizard", help="guided: unmount, check, grow partition, grow filesystem, verify")
    a.add_argument("target", nargs="?",
                   help="exFAT partition (/dev/sda1), disk (/dev/sda), or image file; "
                        "default: pick from the exFAT partitions found")
    a.add_argument("--partition", type=int, help="partition number inside a disk image file")
    a.add_argument("--yes", action="store_true", help="answer yes to every question (unattended)")
    a.add_argument("--backup-dir", help="where to save undo data and the partition table "
                                        "(default: your home directory)")
    a = sub.add_parser("restore", help="revert a grow using its undo file")
    a.add_argument("device")
    a.add_argument("--undo", required=True)
    a.add_argument("--write", action="store_true")
    args = ap.parse_args()
    try:
        {"info": cmd_info, "grow": cmd_grow, "restore": cmd_restore, "wizard": cmd_wizard}[args.cmd](args)
    except FsError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted")


if __name__ == "__main__":
    main()
