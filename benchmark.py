#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║          SYSBENCH — System Performance Tool          ║
║     Cross-platform · CPU · Memory · Disk · Net       ║
╚══════════════════════════════════════════════════════╝
Requires: pip install psutil rich
"""

import os
import sys
import time
import math
import json
import socket
import hashlib
import platform
import threading
import subprocess
import multiprocessing
from datetime import datetime
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
def check_deps():
    missing = []
    for pkg in ("psutil", "rich"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[!] Missing packages: {', '.join(missing)}")
        print(f"    Run: pip install {' '.join(missing)}")
        sys.exit(1)

check_deps()

import psutil
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule
from rich import box

console = Console()
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
IS_MAC     = platform.system() == "Darwin"

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

def fmt_speed(bps):
    return fmt_bytes(bps) + "/s"

def score_label(score, thresholds):
    """Return (colour, label) for a normalised 0-100 score."""
    if score >= thresholds[0]:   return "green",  "Excellent"
    if score >= thresholds[1]:   return "yellow", "Good"
    if score >= thresholds[2]:   return "orange3","Fair"
    return "red", "Needs Improvement"

# ══════════════════════════════════════════════════════════════════════════════
# 1 · SYSTEM INFO
# ══════════════════════════════════════════════════════════════════════════════

def gather_system_info():
    info = {}

    # OS
    info["os_name"]    = platform.system()
    info["os_version"] = platform.version()
    info["os_release"] = platform.release()
    info["hostname"]   = socket.gethostname()
    info["arch"]       = platform.machine()
    info["python"]     = platform.python_version()
    info["timestamp"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Distro (Linux)
    if IS_LINUX:
        try:
            import distro
            info["distro"] = f"{distro.name()} {distro.version()}"
        except ImportError:
            try:
                r = subprocess.run(["lsb_release", "-ds"], capture_output=True, text=True)
                info["distro"] = r.stdout.strip().strip('"')
            except Exception:
                info["distro"] = "Unknown Linux"
    elif IS_WINDOWS:
        info["distro"] = f"Windows {platform.win32_ver()[0]}"
    elif IS_MAC:
        info["distro"] = f"macOS {platform.mac_ver()[0]}"
    else:
        info["distro"] = platform.system()

    # CPU
    info["cpu_name"]      = _cpu_name()
    info["cpu_physical"]  = psutil.cpu_count(logical=False) or 1
    info["cpu_logical"]   = psutil.cpu_count(logical=True)  or 1
    info["cpu_freq_max"]  = getattr(psutil.cpu_freq(), "max", 0) if psutil.cpu_freq() else 0
    info["cpu_freq_cur"]  = getattr(psutil.cpu_freq(), "current", 0) if psutil.cpu_freq() else 0

    # RAM
    vm = psutil.virtual_memory()
    info["ram_total"]     = vm.total
    info["ram_available"] = vm.available
    info["ram_used"]      = vm.used
    info["ram_pct"]       = vm.percent

    # Swap
    sw = psutil.swap_memory()
    info["swap_total"]    = sw.total
    info["swap_used"]     = sw.used

    # Disks
    info["disks"] = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            info["disks"].append({
                "device":     part.device,
                "mountpoint": part.mountpoint,
                "fstype":     part.fstype,
                "total":      usage.total,
                "used":       usage.used,
                "free":       usage.free,
                "pct":        usage.percent,
            })
        except PermissionError:
            pass

    # Network interfaces
    info["net_ifaces"] = []
    for iface, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET:
                info["net_ifaces"].append({"iface": iface, "ip": a.address})

    # GPU (best-effort)
    info["gpu"] = _detect_gpu()

    return info


def _cpu_name():
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    if IS_LINUX:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
        except Exception:
            pass
    if IS_MAC:
        try:
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True)
            return r.stdout.strip()
        except Exception:
            pass
    return platform.processor() or "Unknown CPU"


def _detect_gpu():
    gpus = []
    # nvidia-smi
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                gpus.append({"name": parts[0],
                             "vram": f"{parts[1]} MiB" if len(parts) > 1 else "?",
                             "driver": parts[2] if len(parts) > 2 else "?"})
    except Exception:
        pass
    # lspci (Linux fallback)
    if not gpus and IS_LINUX:
        try:
            r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "VGA" in line or "3D" in line or "Display" in line:
                    gpus.append({"name": line.split(":")[-1].strip(), "vram": "?", "driver": "?"})
        except Exception:
            pass
    return gpus if gpus else [{"name": "Not detected", "vram": "?", "driver": "?"}]

# ══════════════════════════════════════════════════════════════════════════════
# 2 · BENCHMARKS
# ══════════════════════════════════════════════════════════════════════════════

# ── 2a CPU single-core ────────────────────────────────────────────────────────

def _cpu_single_workload(duration=5):
    """Mixed workload: float arithmetic + integer ops + SHA-256 hashing."""
    ops = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration:
        # Float loop
        x = 1.0
        for _ in range(5000):
            x = math.sqrt(x * 1.0000001 + 0.00001)
            x = math.sin(x) * math.cos(x)
        # Integer loop
        acc = 0
        for i in range(1, 5001):
            acc += i * i
        # Hash
        hashlib.sha256(b"benchmark" * 64).hexdigest()
        ops += 1
    elapsed = time.perf_counter() - t0
    return ops, elapsed


def bench_cpu_single():
    ops, elapsed = _cpu_single_workload(duration=5)
    score = ops / elapsed          # ops/sec (raw)
    return {"ops_per_sec": score, "elapsed": elapsed, "iterations": ops}


# ── 2b CPU multi-core ─────────────────────────────────────────────────────────

def _worker(_):
    ops, elapsed = _cpu_single_workload(duration=5)
    return ops / elapsed


def bench_cpu_multi():
    cores = psutil.cpu_count(logical=True) or 1
    t0 = time.perf_counter()
    with multiprocessing.Pool(cores) as pool:
        results = pool.map(_worker, range(cores))
    elapsed = time.perf_counter() - t0
    total = sum(results)
    return {"total_ops_per_sec": total, "cores_used": cores, "elapsed": elapsed}


# ── 2c Memory bandwidth ───────────────────────────────────────────────────────

def bench_memory():
    SIZE = 128 * 1024 * 1024  # 128 MB
    results = {}

    # Sequential write
    t0 = time.perf_counter()
    buf = bytearray(SIZE)
    for i in range(0, SIZE, 4096):
        buf[i:i+4096] = b'\xAB' * 4096
    write_time = time.perf_counter() - t0
    results["seq_write_bps"] = SIZE / write_time

    # Sequential read
    t0 = time.perf_counter()
    _ = sum(buf[i] for i in range(0, SIZE, 4096))
    read_time = time.perf_counter() - t0
    results["seq_read_bps"] = SIZE / read_time

    # Latency (random access)
    import array, random
    arr = array.array('l', range(65536))
    indices = random.sample(range(len(arr)), 10000)
    t0 = time.perf_counter()
    for idx in indices:
        _ = arr[idx]
    results["latency_us"] = (time.perf_counter() - t0) / 10000 * 1e6

    del buf
    return results


# ── 2d Disk I/O ───────────────────────────────────────────────────────────────

def bench_disk(tmp_path=None):
    if tmp_path is None:
        tmp_path = Path(os.path.expanduser("~")) / ".sysbench_tmp"
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    test_file = tmp_path / "bench.bin"

    CHUNK   = 4 * 1024 * 1024   # 4 MB
    TOTAL   = 256 * 1024 * 1024 # 256 MB
    data    = os.urandom(CHUNK)
    results = {}

    # Sequential write
    t0 = time.perf_counter()
    with open(test_file, "wb") as f:
        for _ in range(TOTAL // CHUNK):
            f.write(data)
        f.flush()
        os.fsync(f.fileno())
    results["seq_write_bps"] = TOTAL / (time.perf_counter() - t0)

    # Sequential read
    t0 = time.perf_counter()
    with open(test_file, "rb") as f:
        while f.read(CHUNK):
            pass
    results["seq_read_bps"] = TOTAL / (time.perf_counter() - t0)

    # Random 4K read IOPS
    IOPS_ROUNDS = 500
    t0 = time.perf_counter()
    with open(test_file, "rb") as f:
        file_size = test_file.stat().st_size
        import random
        for _ in range(IOPS_ROUNDS):
            offset = random.randint(0, file_size - 4096) & ~4095
            f.seek(offset)
            f.read(4096)
    elapsed = time.perf_counter() - t0
    results["rand_read_iops"] = IOPS_ROUNDS / elapsed

    test_file.unlink()
    return results


# ── 2e Network latency ────────────────────────────────────────────────────────

def bench_network():
    results = {}
    hosts = [("8.8.8.8", 53), ("1.1.1.1", 53), ("9.9.9.9", 53)]
    latencies = []
    for host, port in hosts:
        try:
            t0 = time.perf_counter()
            with socket.create_connection((host, port), timeout=2):
                pass
            latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass
    if latencies:
        results["avg_latency_ms"] = sum(latencies) / len(latencies)
        results["min_latency_ms"] = min(latencies)
        results["reachable"]      = True
    else:
        results["avg_latency_ms"] = -1
        results["min_latency_ms"] = -1
        results["reachable"]      = False

    # Localhost throughput
    PORT = 54321
    SIZE = 32 * 1024 * 1024
    sent = [0]

    def server():
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", PORT))
            s.listen(1)
            conn, _ = s.accept()
            with conn:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break

    t = threading.Thread(target=server, daemon=True)
    t.start()
    time.sleep(0.1)
    try:
        with socket.socket() as c:
            c.connect(("127.0.0.1", PORT))
            data  = b'\x00' * 65536
            t0    = time.perf_counter()
            total = 0
            while total < SIZE:
                n = c.send(data)
                total += n
            elapsed = time.perf_counter() - t0
            results["localhost_bps"] = total / elapsed
    except Exception:
        results["localhost_bps"] = 0

    return results

# ══════════════════════════════════════════════════════════════════════════════
# 3 · SCORING
# ══════════════════════════════════════════════════════════════════════════════

def compute_scores(sysinfo, cpu_s, cpu_m, mem, disk, net):
    """Normalise raw numbers to 0-100 scores."""

    # CPU single: ~50 ops/s = 50 pts; ~200 = 100 pts
    cpu_s_score = min(100, cpu_s["ops_per_sec"] / 2.0)

    # CPU multi: ~200 total ops/s / core = 100 pts
    cpu_m_score = min(100, (cpu_m["total_ops_per_sec"] / cpu_m["cores_used"]) / 2.0)

    # Memory: 5 GB/s seq write = 100 pts
    mem_score = min(100, mem["seq_write_bps"] / (5e9 / 100))

    # Disk seq write: 500 MB/s = 100 pts  (NVMe parity)
    disk_score = min(100, disk["seq_write_bps"] / (500e6 / 100))

    # Net latency: <5ms = 100, 200ms = 0
    if net["reachable"] and net["avg_latency_ms"] > 0:
        net_score = max(0, min(100, 100 - (net["avg_latency_ms"] - 5) * 0.5))
    else:
        net_score = 0

    overall = (cpu_s_score * 0.25 + cpu_m_score * 0.25 +
               mem_score   * 0.20 + disk_score  * 0.20 +
               net_score   * 0.10)

    return {
        "cpu_single": round(cpu_s_score, 1),
        "cpu_multi":  round(cpu_m_score,  1),
        "memory":     round(mem_score,    1),
        "disk":       round(disk_score,   1),
        "network":    round(net_score,    1),
        "overall":    round(overall,      1),
    }

# ══════════════════════════════════════════════════════════════════════════════
# 4 · RICH OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

BANNER = r"""
  ███████╗██╗   ██╗███████╗██████╗ ███████╗███╗   ██╗ ██████╗██╗  ██╗
  ██╔════╝╚██╗ ██╔╝██╔════╝██╔══██╗██╔════╝████╗  ██║██╔════╝██║  ██║
  ███████╗ ╚████╔╝ ███████╗██████╔╝█████╗  ██╔██╗ ██║██║     ███████║
  ╚════██║  ╚██╔╝  ╚════██║██╔══██╗██╔══╝  ██║╚██╗██║██║     ██╔══██║
  ███████║   ██║   ███████║██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║
  ╚══════╝   ╚═╝   ╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝
"""

def print_banner():
    console.print(Text(BANNER, style="bold cyan"))
    console.print(
        Panel(
            f"[bold]Cross-Platform System Performance Tool[/bold]\n"
            f"[dim]Running on [cyan]{platform.system()} {platform.release()}[/cyan] · "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
            border_style="cyan",
            expand=False,
        )
    )


def print_system_info(info):
    console.print(Rule("[bold cyan]SYSTEM INFORMATION[/bold cyan]", style="cyan"))

    # OS table
    t = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t.add_column("Key",   style="bold dim", width=20)
    t.add_column("Value", style="white")

    t.add_row("OS",         f"[cyan]{info['distro']}[/cyan]")
    t.add_row("Kernel",     info['os_release'])
    t.add_row("Architecture", info['arch'])
    t.add_row("Hostname",   info['hostname'])
    t.add_row("Python",     info['python'])

    console.print(t)

    # CPU
    console.print()
    console.print("[bold]CPU[/bold]")
    t2 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t2.add_column("Key",   style="bold dim", width=20)
    t2.add_column("Value", style="white")
    t2.add_row("Model",    f"[yellow]{info['cpu_name']}[/yellow]")
    t2.add_row("Cores (Physical)", str(info['cpu_physical']))
    t2.add_row("Threads",          str(info['cpu_logical']))
    freq_max = f"{info['cpu_freq_max']:.0f} MHz" if info['cpu_freq_max'] else "N/A"
    freq_cur = f"{info['cpu_freq_cur']:.0f} MHz" if info['cpu_freq_cur'] else "N/A"
    t2.add_row("Max Freq", freq_max)
    t2.add_row("Cur Freq", freq_cur)
    console.print(t2)

    # RAM
    console.print()
    console.print("[bold]Memory[/bold]")
    t3 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t3.add_column("Key",   style="bold dim", width=20)
    t3.add_column("Value", style="white")
    t3.add_row("Total RAM",     fmt_bytes(info['ram_total']))
    t3.add_row("Used / Free",   f"{fmt_bytes(info['ram_used'])} / {fmt_bytes(info['ram_available'])}")
    t3.add_row("Usage",         f"[{'red' if info['ram_pct']>85 else 'green'}]{info['ram_pct']}%[/]")
    t3.add_row("Swap Total",    fmt_bytes(info['swap_total']))
    t3.add_row("Swap Used",     fmt_bytes(info['swap_used']))
    console.print(t3)

    # Disks
    console.print()
    console.print("[bold]Storage[/bold]")
    td = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim")
    td.add_column("Device",     style="cyan")
    td.add_column("Mount",      style="white")
    td.add_column("FS",         style="dim")
    td.add_column("Total",      justify="right")
    td.add_column("Used",       justify="right")
    td.add_column("Free",       justify="right")
    td.add_column("Usage",      justify="right")
    for d in info['disks']:
        colour = "red" if d['pct'] > 85 else ("yellow" if d['pct'] > 65 else "green")
        td.add_row(
            d['device'], d['mountpoint'], d['fstype'],
            fmt_bytes(d['total']), fmt_bytes(d['used']), fmt_bytes(d['free']),
            f"[{colour}]{d['pct']}%[/{colour}]"
        )
    console.print(td)

    # GPU
    console.print()
    console.print("[bold]GPU[/bold]")
    for g in info['gpu']:
        console.print(f"  [magenta]{g['name']}[/magenta]  VRAM: {g['vram']}  Driver: {g['driver']}")

    # Network
    console.print()
    console.print("[bold]Network Interfaces[/bold]")
    for iface in info['net_ifaces']:
        console.print(f"  [cyan]{iface['iface']}[/cyan]  {iface['ip']}")


def _score_bar(score):
    filled = int(score / 5)      # 20 chars = 100
    bar    = "█" * filled + "░" * (20 - filled)
    colour, label = score_label(score, [85, 65, 40])
    return f"[{colour}]{bar}[/{colour}] [{colour}]{score:5.1f}/100  {label}[/{colour}]"


def print_results(cpu_s, cpu_m, mem, disk, net, scores):
    console.print()
    console.print(Rule("[bold cyan]BENCHMARK RESULTS[/bold cyan]", style="cyan"))

    # ── CPU ──
    console.print()
    console.print("[bold underline]CPU[/bold underline]")
    t = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t.add_column("Metric", style="bold dim", width=30)
    t.add_column("Value",  style="white",    width=22)
    t.add_column("Score",  style="white",    width=40)
    t.add_row("Single-core ops/sec",
              f"{cpu_s['ops_per_sec']:,.1f}",
              _score_bar(scores['cpu_single']))
    t.add_row(f"Multi-core ops/sec ({cpu_m['cores_used']} threads)",
              f"{cpu_m['total_ops_per_sec']:,.1f}",
              _score_bar(scores['cpu_multi']))
    console.print(t)

    # ── Memory ──
    console.print()
    console.print("[bold underline]Memory Bandwidth[/bold underline]")
    t2 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t2.add_column("Metric", style="bold dim", width=30)
    t2.add_column("Value",  style="white",    width=22)
    t2.add_column("Score",  style="white",    width=40)
    t2.add_row("Sequential Write",
               fmt_speed(mem['seq_write_bps']),
               _score_bar(scores['memory']))
    t2.add_row("Sequential Read",
               fmt_speed(mem['seq_read_bps']), "")
    t2.add_row("Random Access Latency",
               f"{mem['latency_us']:.3f} µs", "")
    console.print(t2)

    # ── Disk ──
    console.print()
    console.print("[bold underline]Disk I/O[/bold underline]")
    t3 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t3.add_column("Metric", style="bold dim", width=30)
    t3.add_column("Value",  style="white",    width=22)
    t3.add_column("Score",  style="white",    width=40)
    t3.add_row("Sequential Write",
               fmt_speed(disk['seq_write_bps']),
               _score_bar(scores['disk']))
    t3.add_row("Sequential Read",
               fmt_speed(disk['seq_read_bps']), "")
    t3.add_row("Random 4K Read IOPS",
               f"{disk['rand_read_iops']:.0f} IOPS", "")
    console.print(t3)

    # ── Network ──
    console.print()
    console.print("[bold underline]Network[/bold underline]")
    t4 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0,1))
    t4.add_column("Metric", style="bold dim", width=30)
    t4.add_column("Value",  style="white",    width=22)
    t4.add_column("Score",  style="white",    width=40)
    if net['reachable']:
        t4.add_row("External Latency (avg)",
                   f"{net['avg_latency_ms']:.1f} ms",
                   _score_bar(scores['network']))
        t4.add_row("External Latency (min)",
                   f"{net['min_latency_ms']:.1f} ms", "")
    else:
        t4.add_row("External Latency", "[red]Unreachable[/red]",
                   _score_bar(0))
    t4.add_row("Localhost Throughput",
               fmt_speed(net['localhost_bps']), "")
    console.print(t4)

    # ── Overall ──
    console.print()
    colour, label = score_label(scores['overall'], [80, 60, 40])
    console.print(
        Panel(
            f"[bold]OVERALL SCORE[/bold]\n\n"
            f"  [{colour}]{'█' * int(scores['overall']/5)}{'░'*(20-int(scores['overall']/5))}[/{colour}]\n\n"
            f"  [{colour}][bold]{scores['overall']:.1f} / 100[/bold]  —  {label}[/{colour}]",
            border_style=colour,
            expand=False,
            width=50,
        )
    )


def save_json(sysinfo, results, scores, stem):
    out = {
        "generated_at": sysinfo["timestamp"],
        "system":       sysinfo,
        "results":      results,
        "scores":       scores,
    }
    path = Path.home() / f"{stem}.json"
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# 5 · HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _score_colour(score):
    if score >= 85: return "#22c55e"
    if score >= 65: return "#eab308"
    if score >= 40: return "#f97316"
    return "#ef4444"

def _score_label(score):
    if score >= 85: return "Excellent"
    if score >= 65: return "Good"
    if score >= 40: return "Fair"
    return "Needs Improvement"

def _gauge_svg(score, size=120):
    c      = _score_colour(score)
    r      = 46
    cx, cy = size // 2, size // 2 + 10
    arc_len  = math.pi * r
    dash_val = arc_len * (score / 100)
    dash_rest = arc_len - dash_val
    return f"""<svg width="{size}" height="{size//2+24}" viewBox="0 0 {size} {size//2+24}">
  <path d="M{cx-r},{cy} A{r},{r} 0 0,1 {cx+r},{cy}"
        fill="none" stroke="#2d2d2d" stroke-width="10" stroke-linecap="round"/>
  <path d="M{cx-r},{cy} A{r},{r} 0 0,1 {cx+r},{cy}"
        fill="none" stroke="{c}" stroke-width="10" stroke-linecap="round"
        stroke-dasharray="{dash_val:.1f} {dash_rest+arc_len:.1f}"/>
  <text x="{cx}" y="{cy+4}" text-anchor="middle"
        font-size="18" font-weight="700" fill="{c}">{score:.0f}</text>
  <text x="{cx}" y="{cy+18}" text-anchor="middle"
        font-size="9" fill="#888">{_score_label(score)}</text>
</svg>"""

def _bar_html(score):
    c = _score_colour(score)
    return (f'<div class="bar-wrap">'
            f'<div class="bar-fill" style="width:{score}%;background:{c}"></div>'
            f'</div>'
            f'<span class="bar-val" style="color:{c}">{score:.1f}</span>')

def save_html(sysinfo, bench, scores, stem):
    ts   = sysinfo["timestamp"]
    s    = scores
    cpu  = bench["cpu_single"]
    cpum = bench["cpu_multi"]
    mem  = bench["memory"]
    disk = bench["disk"]
    net  = bench["network"]

    disk_rows = ""
    for d in sysinfo["disks"]:
        c = "#ef4444" if d["pct"] > 85 else ("#eab308" if d["pct"] > 65 else "#22c55e")
        disk_rows += (f"<tr><td>{d['device']}</td><td>{d['mountpoint']}</td>"
                      f"<td>{d['fstype']}</td><td>{fmt_bytes(d['total'])}</td>"
                      f"<td>{fmt_bytes(d['used'])}</td><td>{fmt_bytes(d['free'])}</td>"
                      f"<td><span style='color:{c};font-weight:600'>{d['pct']}%</span></td></tr>")

    gpu_rows = "".join(
        f"<tr><td>{g['name']}</td><td>{g['vram']}</td><td>{g['driver']}</td></tr>"
        for g in sysinfo["gpu"])
    net_rows = "".join(
        f"<tr><td>{i['iface']}</td><td>{i['ip']}</td></tr>"
        for i in sysinfo["net_ifaces"])

    net_latency = (f"{net['avg_latency_ms']:.1f} ms avg / {net['min_latency_ms']:.1f} ms min"
                   if net["reachable"] else "Unreachable")

    gauges = "".join(
        f'<div class="gauge-cell"><div class="gauge-label">{lbl}</div>{_gauge_svg(sc)}</div>'
        for lbl, sc in [("CPU Single", s["cpu_single"]), ("CPU Multi", s["cpu_multi"]),
                        ("Memory", s["memory"]), ("Disk", s["disk"]), ("Network", s["network"])])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SysBench Report — {ts}</title>
<style>
  :root {{
    --bg:#0f0f13; --surface:#18181f; --border:#2a2a35;
    --text:#e2e2e8; --muted:#6b6b80; --accent:#38bdf8;
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
        font-size:14px;line-height:1.6;padding:32px 16px}}
  h1{{font-size:2rem;font-weight:800;color:var(--accent);letter-spacing:-0.5px}}
  h2{{font-size:1rem;font-weight:700;color:var(--accent);text-transform:uppercase;
      letter-spacing:1px;border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:14px}}
  .wrap{{max-width:960px;margin:0 auto}}
  .card{{background:var(--surface);border:1px solid var(--border);
         border-radius:12px;padding:24px;margin-bottom:24px}}
  .meta{{color:var(--muted);font-size:12px;margin-top:4px}}
  .gauges{{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-around}}
  .gauge-cell{{text-align:center}}
  .gauge-label{{font-size:11px;color:var(--muted);text-transform:uppercase;
                letter-spacing:0.5px;margin-bottom:4px}}
  .overall{{display:flex;align-items:center;gap:32px;margin-bottom:24px}}
  .overall-score{{font-size:4rem;font-weight:900;line-height:1}}
  .kv{{display:grid;grid-template-columns:180px 1fr;gap:4px 16px}}
  .kv-key{{color:var(--muted)}}
  .kv-val{{font-weight:500}}
  .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:20px}}
  .section-label{{font-size:.75rem;color:var(--muted);text-transform:uppercase;
                  letter-spacing:0.5px;margin-bottom:8px}}
  .bench-section{{margin-bottom:20px}}
  .bench-section h3{{font-size:.85rem;font-weight:700;color:var(--muted);
                     text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}}
  .bench-row{{display:grid;grid-template-columns:220px 150px 1fr;align-items:center;
              gap:12px;padding:6px 0;border-bottom:1px solid var(--border)}}
  .bench-row:last-child{{border-bottom:none}}
  .bench-value{{font-weight:700;font-variant-numeric:tabular-nums;color:var(--accent)}}
  .bar-wrap{{display:inline-block;width:120px;height:8px;background:#2a2a35;
             border-radius:4px;vertical-align:middle}}
  .bar-fill{{height:8px;border-radius:4px}}
  .bar-val{{font-size:12px;font-weight:700;margin-left:8px}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;
      letter-spacing:0.5px;padding:6px 8px;border-bottom:1px solid var(--border)}}
  td{{padding:6px 8px;border-bottom:1px solid #1f1f28}}
  tr:last-child td{{border-bottom:none}}
  footer{{text-align:center;color:var(--muted);font-size:11px;margin-top:32px}}
</style>
</head>
<body>
<div class="wrap">

  <div class="card">
    <h1>&#x26A1; SysBench Report</h1>
    <p class="meta">Generated on {ts} &nbsp;&middot;&nbsp; {sysinfo['distro']} &nbsp;&middot;&nbsp; {sysinfo['hostname']}</p>
  </div>

  <div class="card">
    <h2>Overall Score</h2>
    <div class="overall">
      <div class="overall-score" style="color:{_score_colour(s['overall'])}">{s['overall']:.1f}</div>
      <div>
        <div style="font-size:1.1rem;font-weight:600;color:{_score_colour(s['overall'])}">{_score_label(s['overall'])}</div>
        <div class="meta">Weighted: CPU 50% &middot; Memory 20% &middot; Disk 20% &middot; Network 10%</div>
      </div>
    </div>
    <div class="gauges">{gauges}</div>
  </div>

  <div class="card">
    <h2>System Information</h2>
    <div class="two-col">
      <div>
        <div class="section-label">OS</div>
        <div class="kv">
          <span class="kv-key">OS</span>        <span class="kv-val">{sysinfo['distro']}</span>
          <span class="kv-key">Kernel</span>    <span class="kv-val">{sysinfo['os_release']}</span>
          <span class="kv-key">Arch</span>      <span class="kv-val">{sysinfo['arch']}</span>
          <span class="kv-key">Hostname</span>  <span class="kv-val">{sysinfo['hostname']}</span>
          <span class="kv-key">Python</span>    <span class="kv-val">{sysinfo['python']}</span>
        </div>
      </div>
      <div>
        <div class="section-label">CPU</div>
        <div class="kv">
          <span class="kv-key">Model</span>     <span class="kv-val">{sysinfo['cpu_name']}</span>
          <span class="kv-key">Cores (P/L)</span><span class="kv-val">{sysinfo['cpu_physical']} / {sysinfo['cpu_logical']}</span>
          <span class="kv-key">Max Freq</span>  <span class="kv-val">{sysinfo['cpu_freq_max']:.0f} MHz</span>
          <span class="kv-key">Cur Freq</span>  <span class="kv-val">{sysinfo['cpu_freq_cur']:.0f} MHz</span>
        </div>
      </div>
    </div>
    <div class="two-col">
      <div>
        <div class="section-label">Memory</div>
        <div class="kv">
          <span class="kv-key">Total RAM</span>   <span class="kv-val">{fmt_bytes(sysinfo['ram_total'])}</span>
          <span class="kv-key">Used</span>         <span class="kv-val">{fmt_bytes(sysinfo['ram_used'])} ({sysinfo['ram_pct']}%)</span>
          <span class="kv-key">Available</span>   <span class="kv-val">{fmt_bytes(sysinfo['ram_available'])}</span>
          <span class="kv-key">Swap Total</span>  <span class="kv-val">{fmt_bytes(sysinfo['swap_total'])}</span>
          <span class="kv-key">Swap Used</span>   <span class="kv-val">{fmt_bytes(sysinfo['swap_used'])}</span>
        </div>
      </div>
      <div>
        <div class="section-label">GPU</div>
        <table><thead><tr><th>Name</th><th>VRAM</th><th>Driver</th></tr></thead>
        <tbody>{gpu_rows}</tbody></table>
      </div>
    </div>
    <div class="section-label">Storage</div>
    <table><thead><tr><th>Device</th><th>Mount</th><th>FS</th><th>Total</th><th>Used</th><th>Free</th><th>Usage</th></tr></thead>
    <tbody>{disk_rows}</tbody></table>
    <div style="margin-top:20px">
      <div class="section-label">Network Interfaces</div>
      <table><thead><tr><th>Interface</th><th>IP Address</th></tr></thead>
      <tbody>{net_rows}</tbody></table>
    </div>
  </div>

  <div class="card">
    <h2>Benchmark Results</h2>
    <div class="bench-section">
      <h3>CPU</h3>
      <div class="bench-row">
        <span>Single-Core ops/sec</span>
        <span class="bench-value">{cpu['ops_per_sec']:,.1f}</span>
        <span>{_bar_html(s['cpu_single'])}</span>
      </div>
      <div class="bench-row">
        <span>Multi-Core ops/sec ({cpum['cores_used']} threads)</span>
        <span class="bench-value">{cpum['total_ops_per_sec']:,.1f}</span>
        <span>{_bar_html(s['cpu_multi'])}</span>
      </div>
    </div>
    <div class="bench-section">
      <h3>Memory Bandwidth</h3>
      <div class="bench-row">
        <span>Sequential Write</span>
        <span class="bench-value">{fmt_speed(mem['seq_write_bps'])}</span>
        <span>{_bar_html(s['memory'])}</span>
      </div>
      <div class="bench-row">
        <span>Sequential Read</span>
        <span class="bench-value">{fmt_speed(mem['seq_read_bps'])}</span><span></span>
      </div>
      <div class="bench-row">
        <span>Random Access Latency</span>
        <span class="bench-value">{mem['latency_us']:.3f} &micro;s</span><span></span>
      </div>
    </div>
    <div class="bench-section">
      <h3>Disk I/O</h3>
      <div class="bench-row">
        <span>Sequential Write</span>
        <span class="bench-value">{fmt_speed(disk['seq_write_bps'])}</span>
        <span>{_bar_html(s['disk'])}</span>
      </div>
      <div class="bench-row">
        <span>Sequential Read</span>
        <span class="bench-value">{fmt_speed(disk['seq_read_bps'])}</span><span></span>
      </div>
      <div class="bench-row">
        <span>Random 4K Read IOPS</span>
        <span class="bench-value">{disk['rand_read_iops']:.0f} IOPS</span><span></span>
      </div>
    </div>
    <div class="bench-section">
      <h3>Network</h3>
      <div class="bench-row">
        <span>External Latency</span>
        <span class="bench-value">{net_latency}</span>
        <span>{_bar_html(s['network'])}</span>
      </div>
      <div class="bench-row">
        <span>Localhost Throughput</span>
        <span class="bench-value">{fmt_speed(net['localhost_bps'])}</span><span></span>
      </div>
    </div>
  </div>

</div>
<footer>SysBench &middot; {ts}</footer>
</body>
</html>"""

    path = Path.home() / f"{stem}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# 6 · MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print_banner()

    console.print()
    with console.status("[cyan]Gathering system information…[/cyan]"):
        sysinfo = gather_system_info()

    print_system_info(sysinfo)

    console.print()
    console.print(Rule("[bold cyan]RUNNING BENCHMARKS[/bold cyan]", style="cyan"))
    console.print("[dim]Each test is designed to stress a specific subsystem.[/dim]\n")

    bench_results = {}
    steps = [
        ("CPU Single-Core",  bench_cpu_single, "cpu_single"),
        ("CPU Multi-Core",   bench_cpu_multi,  "cpu_multi"),
        ("Memory Bandwidth", bench_memory,     "memory"),
        ("Disk I/O",         bench_disk,       "disk"),
        ("Network",          bench_network,    "network"),
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        for label, fn, key in steps:
            task = progress.add_task(f"[cyan]{label}[/cyan]…", total=None)
            bench_results[key] = fn()
            progress.update(task, completed=1, total=1,
                            description=f"[green]✓ {label}[/green]")

    scores = compute_scores(
        sysinfo,
        bench_results["cpu_single"],
        bench_results["cpu_multi"],
        bench_results["memory"],
        bench_results["disk"],
        bench_results["network"],
    )

    print_results(
        bench_results["cpu_single"],
        bench_results["cpu_multi"],
        bench_results["memory"],
        bench_results["disk"],
        bench_results["network"],
        scores,
    )

    # ── Save outputs ──────────────────────────────────────────────────────────
    stem = f"sysbench_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    console.print()
    json_path = save_json(sysinfo, bench_results, scores, stem)
    html_path = save_html(sysinfo, bench_results, scores, stem)
    console.print(f"[dim]JSON report → [cyan]{json_path}[/cyan][/dim]")
    console.print(f"[dim]HTML report → [cyan]{html_path}[/cyan][/dim]")
    console.print()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()