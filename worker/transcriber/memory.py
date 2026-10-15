"""Сколько памяти воркер держит на самом деле — так, как её считает «Мониторинг системы».

RSS тут не годится: Metal-буферы MLX и сжатые страницы в него не попадают, и процесс
с RSS 50 МБ спокойно держит 11 ГБ. Честная цифра — phys_footprint из proc_pid_rusage.
"""
from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("transcriber")

_RUSAGE_INFO_V2 = 2


class _RUsageInfoV2(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins",
        "ri_wired_size", "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime",
        "ri_proc_exit_abstime", "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
        "ri_child_interrupt_wkups", "ri_child_pageins", "ri_child_elapsed_abstime",
        "ri_diskio_bytesread", "ri_diskio_byteswritten")]


_libproc = None


def footprint_mb() -> float:
    """Physical footprint процесса в МБ; 0.0, если узнать не вышло (не macOS и т.п.)."""
    global _libproc
    try:
        libproc = _libproc = _libproc or ctypes.CDLL("/usr/lib/libproc.dylib")
        info = _RUsageInfoV2()
        if libproc.proc_pid_rusage(os.getpid(), _RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
            return 0.0
        return info.ri_phys_footprint / 2 ** 20
    except Exception:  # noqa: BLE001 — замер памяти не должен ронять воркер
        return 0.0


def mlx_summary() -> str:
    """Активная память и кеш MLX — чтобы по логу было видно, кто растёт: веса, кеш или прочее."""
    try:
        import mlx.core as mx

        return (f"mlx active {mx.get_active_memory() / 2 ** 20:.0f} MB, "
                f"cache {mx.get_cache_memory() / 2 ** 20:.0f} MB")
    except Exception:  # noqa: BLE001
        return "mlx n/a"
