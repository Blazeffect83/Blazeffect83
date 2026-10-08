"""Read-only environment audit (Part 2). Changes nothing on the machine."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from ..observability import metrics


def _cmd(argv: list[str], timeout: int = 10) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def environment_report(settings) -> dict:
    os_release = {}
    for line in (_read("/etc/os-release") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            os_release[k] = v.strip('"')
    mem_pct, mem_avail = metrics.memory()
    meminfo = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        meminfo[k] = v.strip()
    model = _read("/proc/device-tree/model")
    cpuinfo = _read("/proc/cpuinfo") or ""
    cpu_model = next((ln.split(":", 1)[1].strip() for ln in cpuinfo.splitlines()
                      if ln.lower().startswith(("model name", "hardware", "cpu part"))), None)
    disk_target = settings.data_dir if settings.data_dir.exists() else Path("/")
    du = shutil.disk_usage(disk_target)
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        net = "outbound TCP 443 reachable"
    except OSError as exc:
        net = f"outbound TCP 443 failed: {exc}"
    sandbox_probe = None
    if shutil.which("bwrap"):
        from ..security.sandbox import Sandbox
        sb = Sandbox("bwrap")
        sandbox_probe = sb.status()
    return {
        "os": os_release.get("PRETTY_NAME") or platform.platform(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "device_model": model.strip("\x00") if model else None,
        "cpu": cpu_model, "cpu_count": os.cpu_count(),
        "memory_total": meminfo.get("MemTotal"), "memory_available_mb": mem_avail, "memory_used_percent": mem_pct,
        "swap_total": meminfo.get("SwapTotal"),
        "data_dir": str(settings.data_dir), "disk_total_gb": round(du.total / 1e9, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
        "mounts": [ln for ln in (_read("/proc/mounts") or "").splitlines()
                   if ln.split()[0].startswith(("/dev/", "PARTUUID")) or " / " in ln][:20],
        "cpu_temp_c": metrics.cpu_temperature(),
        "network": net,
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "docker": _cmd(["docker", "--version"]), "docker_compose": _cmd(["docker", "compose", "version"]),
        "bubblewrap": _cmd(["bwrap", "--version"]), "sandbox_probe": sandbox_probe,
        "systemd": _cmd(["systemctl", "--version"]).splitlines()[0] if _cmd(["systemctl", "--version"]) else None,
        "listening_ports": (_cmd(["ss", "-ltnH"]) or "").splitlines()[:40],
        "ai_runtimes": {name: bool(shutil.which(name)) for name in ("ollama", "llama-server", "llama-cli", "vllm")},
        "ollama_models": _cmd(["ollama", "list"]) if shutil.which("ollama") else None,
        "existing_aegis_dirs": [p for p in ("/opt/aegis", "/var/lib/aegis", "/etc/aegis") if Path(p).exists()],
        "userns_restricted": _read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
    }


def format_report(r: dict) -> str:
    lines = ["AEGIS environment report (read-only)", "=" * 40]
    for k, v in r.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"    {x}" for x in v] or ["    (none)"]
        else:
            lines.append(f"{k}: {v}")
    total_kb = int((r.get("memory_total") or "0 kB").split()[0])
    lines.append("")
    if total_kb and total_kb < 6 * 1024 * 1024:
        lines.append("note: <6 GB RAM — use a cloud provider or a small (≤3B, 4-bit) local model only.")
    if r.get("userns_restricted") == "1" and not (r.get("sandbox_probe") or {}).get("strong_isolation"):
        lines.append("note: unprivileged user namespaces are restricted by AppArmor; see docs/INSTALL.md "
                     "(bubblewrap AppArmor profile) to enable the strong sandbox.")
    return "\n".join(lines)
