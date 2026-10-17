#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Giovanni Andrea Binda <322129496+giobinda@users.noreply.github.com>
# Runs the whole test suite. Needs exfatprogs, util-linux and udisksctl
# (a desktop session where udisks lets you set up loop devices).
set -uo pipefail
cd "$(dirname "$0")"
WORK=${TMPDIR:-/tmp}/exfat-grow-tests.$$
mkdir -p "$WORK"
fail=0
check() {
  local name=$1; shift
  printf '%-40s ' "$name"
  if out=$("$@" 2>&1) && ! grep -qE "error|Traceback" <<<"$out" \
     && [ "$(grep -c OK <<<"$out")" -ge "$EXPECT" ]; then
    echo PASS
  else
    echo FAIL; echo "$out" | tail -20 | sed 's/^/    /'; fail=1
  fi
}
EXPECT=3
check "shift, 4K clusters"            ./run_test.sh "$WORK/a" 1G 3G -c 4K
check "shift, 4K sectors"             ./run_test.sh "$WORK/b" 1G 3G -s 4096 -c 8K
check "no shift, 128K clusters"       ./run_test.sh "$WORK/c" 4664M 9539M -c 128K
check "no shift, 4K sectors"          ./run_test.sh "$WORK/d" 1G 2500M -s 4096 -c 32K
check "no shift, 16M alignment"       ./run_test.sh "$WORK/e" 1G 2G -c 64K -b 16M
EXPECT=1
check "wizard, MBR"                   ./wizard_test.sh "$WORK/f" dos
check "wizard, GPT"                   ./wizard_test.sh "$WORK/g" gpt
check "wizard, GPT + next partition"  ./wizard_test.sh "$WORK/h" gpt --second-partition
rm -rf "$WORK"
exit $fail
