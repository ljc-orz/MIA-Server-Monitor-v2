#v26.5.9:2037

import json
import os
import random
import socket
import sys
from urllib.request import urlopen

JINFO_HOST = "172.18.167.15"
JINFO_PORT = 2223
JINFO_URL = f"http://{JINFO_HOST}:{JINFO_PORT}/jinfo"
IDLE_SCORE_THRESHOLD = 10.0  # sym:IDLE_SCORE_THRESHOLD
BUSY_SCORE_THRESHOLD = 50.0  # sym:BUSY_SCORE_THRESHOLD
EXCLUDE_SERVERS = []  # sym:EXCLUDE_SERVERS
MAX_RECOMMENDATIONS_PER_SERVER = 1

MY_IP = socket.gethostbyname(socket.gethostname())


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value, lower=0.0, upper=100.0):
    return max(lower, min(upper, value))


def ratio_percent(used, total):
    total = safe_float(total)
    if total <= 0:
        return 0.0
    return clamp(safe_float(used) / total * 100.0)


def process_score(processes, memory_total):
    if not processes:
        return 0.0

    user_processes = [
        process
        for process in processes
        if str(process.get("command", "")).lower() not in {"xorg"}
    ]
    if not user_processes:
        return 0.0

    process_memory = sum(safe_float(process.get("gpu_memory_usage")) for process in user_processes)
    process_memory_percent = ratio_percent(process_memory, memory_total)
    process_count_percent = clamp(len(user_processes) * 12.5)
    return max(process_memory_percent, process_count_percent)


def busy_score(gpu):
    memory_total = gpu.get("memory.total")
    memory_percent = ratio_percent(gpu.get("memory.used"), memory_total)
    utilization_percent = clamp(safe_float(gpu.get("utilization.gpu")))
    power_percent = ratio_percent(gpu.get("power.draw"), gpu.get("enforced.power.limit"))
    temperature_percent = ratio_percent(gpu.get("temperature.gpu"), 90)
    proc_percent = process_score(gpu.get("processes") or [], memory_total)

    # Memory pressure is the strongest signal for whether a new job can fit.
    return (
        memory_percent * 0.50
        + utilization_percent * 0.30
        + power_percent * 0.10
        + proc_percent * 0.07
        + temperature_percent * 0.03
    )


def collect_gpu_scores(jinfo, target_ip=None, exclude_ips=None):
    exclude_ips = set(exclude_ips or [])
    rows = []
    for server in jinfo.get("data", []):
        if server.get("status") != "ok":
            continue

        server_name = server.get("server_name") or server.get("json_data", {}).get("hostname") or "unknown"
        ip = server.get("ip", "")
        if target_ip is not None and ip != target_ip:
            continue
        if ip in exclude_ips:
            continue

        for gpu in server.get("json_data", {}).get("gpus", []):
            memory_percent = ratio_percent(gpu.get("memory.used"), gpu.get("memory.total"))
            rows.append(
                {
                    "score": busy_score(gpu),
                    "server": server_name,
                    "ip": ip,
                    "index": gpu.get("index", "?"),
                    "name": gpu.get("name", "GPU"),
                    "util": clamp(safe_float(gpu.get("utilization.gpu"))),
                    "memory_percent": memory_percent,
                    "memory_used": safe_float(gpu.get("memory.used")),
                    "memory_total": safe_float(gpu.get("memory.total")),
                    "power": safe_float(gpu.get("power.draw")),
                    "power_limit": gpu.get("enforced.power.limit"),
                    "temperature": safe_float(gpu.get("temperature.gpu")),
                    "process_count": len(gpu.get("processes") or []),
                }
            )
    return sorted(rows, key=lambda row: (row["score"], row["server"], row["index"]))


def available_gpu_scores(rows):
    return [row for row in rows if row["score"] < BUSY_SCORE_THRESHOLD]


def choose_gpu(rows):
    candidates = available_gpu_scores(rows)
    if not candidates:
        return None

    idle_candidates = [row for row in candidates if row["score"] < IDLE_SCORE_THRESHOLD]
    if idle_candidates:
        return random.choice(idle_candidates)

    return candidates[0]


def format_gpu_row(row, selected_gpu=None):
    marker = " <-- SELECTED" if row is selected_gpu else ""
    power_limit = row["power_limit"]
    power_text = (
        f"{row['power']:.0f}/{safe_float(power_limit):.0f}W"
        if power_limit is not None
        else f"{row['power']:.0f}W"
    )
    return (
        f"{row['score']:6.2f}  "
        f"{row['server']}:{row['index']}  "
        f"{row['ip']}  "
        f"{row['name']}  "
        f"util={row['util']:.0f}%  "
        f"mem={row['memory_used']:.0f}/{row['memory_total']:.0f}MiB({row['memory_percent']:.1f}%)  "
        f"power={power_text}  "
        f"temp={row['temperature']:.0f}C  "
        f"proc={row['process_count']}"
        f"{marker}"
    )


def print_gpu_scores(rows, selected_gpu=None, title="GPU busy scores, low to high:"):
    print(f"MY_IP: {MY_IP}")
    print(title)
    for row in rows:
        print(format_gpu_row(row, selected_gpu))


def print_remote_recommendations(jinfo):
    exclude_ips = set(EXCLUDE_SERVERS)
    exclude_ips.add(MY_IP)
    remote_rows = collect_gpu_scores(jinfo, exclude_ips=exclude_ips)
    available_rows = available_gpu_scores(remote_rows)
    recommendations_by_ip = {}
    for row in available_rows:
        recommendations_by_ip.setdefault(row["ip"], []).append(row)

    recommended_rows = []
    for rows in recommendations_by_ip.values():
        recommended_rows.extend(rows[:MAX_RECOMMENDATIONS_PER_SERVER])
    recommended_rows.sort(key=lambda row: (row["score"], row["server"], row["index"]))

    print()
    print("Recommended GPUs on other servers:")
    print(f"Excluded server IPs: {sorted(exclude_ips)}")
    if not recommended_rows:
        print(f"No remote GPU has busy score below {BUSY_SCORE_THRESHOLD:.2f}")
        return

    for row in recommended_rows:
        print(format_gpu_row(row))


def main():
    try:
        with urlopen(JINFO_URL) as resp:
            jinfo = json.load(resp)
    except Exception as exc:
        print(f"Failed to fetch jinfo: {exc}")
        return

    gpu_scores = collect_gpu_scores(jinfo, target_ip=MY_IP)
    selected_gpu = choose_gpu(gpu_scores)
    print_gpu_scores(gpu_scores, selected_gpu, title="Local GPU busy scores, low to high:")
    sys.stdout.flush()

    if selected_gpu is None:
        print_remote_recommendations(jinfo)
        sys.stdout.flush()
        raise RuntimeError(
            f"No local GPU has busy score below {BUSY_SCORE_THRESHOLD:.2f}"
        )

    os.environ["CUDA_VISIBLE_DEVICES"] = str(selected_gpu["index"])
    print(
        "Selected GPU: "
        f"{selected_gpu['server']}:{selected_gpu['index']} "
        f"score={selected_gpu['score']:.2f} "
        f"(idle threshold={IDLE_SCORE_THRESHOLD:.2f}, "
        f"busy threshold={BUSY_SCORE_THRESHOLD:.2f})"
    )
    print(f"Using GPU: {os.environ['CUDA_VISIBLE_DEVICES']}")


if __name__ == "__main__":
    main()
