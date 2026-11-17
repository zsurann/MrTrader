#!/usr/bin/env python3
"""Low-priority CPU keepalive for Oracle Always Free idle-reclaim.

Oracle may reclaim an Always Free instance if, over 7 days, P95 CPU
utilization is under 20% AND network utilization is under 20% (the
memory clause applies only to A1 shapes). This process burns a steady
share of one core so guest and OCI CpuUtilization stay around 28% of
all visible CPUs when the desk is idle: safely above 20%, inside the
25-30% target, and with headroom for the desk.

E2.1.Micro is a subcore Always Free shape (1/8 OCPU, two vCPUs visible),
not a formal burstable flex shape. It can use extra CPU opportunistically,
but a busy loop on one vCPU *plus* the desk on the other produces heavy
%steal and can stall cycles; a long stretch well above the 12.5% baseline
also appears to exhaust that extra allowance. This process therefore:

- runs at Nice=19 / SCHED_IDLE / ionice idle (set by systemd)
- should be pinned to a single CPU (CPUAffinity=0)
- sleeps while any thread in memedesk.service is runnable (R) or in
  uninterruptible I/O (D), so the desk does not share the subcore
  budget with a second vCPU of load

OCI CpuUtilization (oci_computeagent, and the hypervisor
oci_vmi_resource_utilization series) is a percent of total guest time,
the same scale as `mpstat` 100-%idle, not a percent of the 12.5%
baseline. A continuous ~28% load is therefore the right target for
P95 > 20%. Most of the 7-day window is the sleep between cycles, so
backing off during a 15-30s cycle does not drop P95 under 20%.

Do not change trading rules from here.

Stop: sudo systemctl disable --now memedesk-keepalive
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# Fraction of all visible CPUs to keep busy when the desk is idle.
TARGET_TOTAL = 0.28
SLICE = 0.10
# How often to refresh the memedesk.service PID list, and how often to
# inspect those few /proc/<pid>/stat files. Walking all of /proc every
# slice would waste the subcore budget on syscalls.
PID_REFRESH = 5.0
BUSY_CHECK = 0.5
PROC = Path("/proc")


def _memedesk_pids() -> list[int]:
    pids: list[int] = []
    try:
        entries = os.listdir(PROC)
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            cg = (PROC / entry / "cgroup").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if "memedesk.service" in cg and "memedesk-keepalive" not in cg:
            pids.append(int(entry))
    return pids


def _any_busy(pids: list[int]) -> bool:
    for pid in pids:
        try:
            st = (PROC / str(pid) / "stat").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        rparen = st.rfind(")")
        if rparen == -1 or rparen + 2 >= len(st):
            continue
        if st[rparen + 2] in ("R", "D"):
            return True
    return False


def main() -> None:
    ncpu = max(1, os.cpu_count() or 2)
    # One thread: 0.56 duty on a 2-vCPU guest => 28% host-wide.
    duty = min(0.85, TARGET_TOTAL * ncpu)
    busy = duty * SLICE
    idle = SLICE - busy
    pids: list[int] = []
    next_pid_refresh = 0.0
    next_busy_check = 0.0
    desk_busy = False

    while True:
        now = time.perf_counter()
        if now >= next_pid_refresh:
            pids = _memedesk_pids()
            next_pid_refresh = now + PID_REFRESH
            next_busy_check = 0.0
        if now >= next_busy_check:
            desk_busy = _any_busy(pids)
            next_busy_check = now + BUSY_CHECK

        if desk_busy:
            time.sleep(SLICE)
            continue

        t0 = time.perf_counter()
        deadline = t0 + busy
        while time.perf_counter() < deadline:
            pass
        leftover = idle - (time.perf_counter() - t0 - busy)
        if leftover > 0:
            time.sleep(leftover)


if __name__ == "__main__":
    main()
