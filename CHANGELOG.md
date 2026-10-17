# Changelog

## 0.1.0 (2026-10-17)

First release.

- `grow`: grow an exFAT filesystem in place, moving the cluster heap start
  when the FAT needs more room.
- `restore`: revert a grow from its undo file.
- `info`: read-only geometry and plan.
- `wizard`: guided unmount, check, partition grow (MBR/GPT), filesystem
  grow and verification; `--yes` for unattended use.
