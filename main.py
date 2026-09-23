import json
import logging
import os
import re
import shlex
import sqlite3
import threading
import fcntl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paramiko
from ansi2html import Ansi2HTMLConverter
from flask import Flask, Response, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
SERVERS_FILE = BASE_DIR / "servers.json"
CHANGELOG_FILE = BASE_DIR / "CHANGELOG.md"
SSH_KEY_FILE = BASE_DIR / "id_rsa_guangxing"
DB_FILE = BASE_DIR / "server_monitor.sqlite3"
COLLECTOR_LOCK_FILE = BASE_DIR / "collector.lock"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
COMMAND = f"TERM=xterm-256color gpustat -P --watch {POLL_INTERVAL_SECONDS}"
COMMAND_JSON = "gpustat --json"
SSH_TIMEOUT_SECONDS = int(os.getenv("SSH_TIMEOUT_SECONDS", "10"))
GPUSTAT_COMMAND_TIMEOUT_SECONDS = float(
    os.getenv("GPUSTAT_COMMAND_TIMEOUT_SECONDS", "8")
)
FALLBACK_POLL_INTERVAL_SECONDS = float(
    os.getenv("FALLBACK_POLL_INTERVAL_SECONDS", "10")
)
FALLBACK_COMMAND_TIMEOUT_SECONDS = float(
    os.getenv("FALLBACK_COMMAND_TIMEOUT_SECONDS", "8")
)
GPUSTAT_RECOVERY_PROBE_SECONDS = float(
    os.getenv("GPUSTAT_RECOVERY_PROBE_SECONDS", "300")
)
RETENTION_MINUTES = int(os.getenv("RETENTION_MINUTES", "10"))
PRUNE_INTERVAL_SECONDS = int(os.getenv("PRUNE_INTERVAL_SECONDS", "60"))
SERVER_CONFIG_REFRESH_SECONDS = int(os.getenv("SERVER_CONFIG_REFRESH_SECONDS", "10"))

PCI_FALLBACK_COMMAND = r"""
boot_id=""
if [ -r /proc/sys/kernel/random/boot_id ]; then
    IFS= read -r boot_id < /proc/sys/kernel/random/boot_id
fi
printf 'BOOT\t%s\n' "$boot_id"

lspci_available=0
lspci_output=""
if command -v lspci >/dev/null 2>&1; then
    lspci_available=1
    if command -v timeout >/dev/null 2>&1; then
        lspci_output=$(timeout 3s lspci -Dnn -d 10de: 2>/dev/null || true)
    else
        lspci_output=$(lspci -Dnn -d 10de: 2>/dev/null || true)
    fi
fi

for dev in /sys/bus/pci/devices/*; do
    [ -r "$dev/vendor" ] || continue
    IFS= read -r vendor < "$dev/vendor"
    [ "$vendor" = "0x10de" ] || continue

    IFS= read -r class_code < "$dev/class"
    case "$class_code" in
        0x0300*|0x0302*) ;;
        *) continue ;;
    esac

    bdf=${dev##*/}
    IFS= read -r device_id < "$dev/device"
    driver="unbound"
    if [ -L "$dev/driver" ]; then
        driver=$(basename "$(readlink -f "$dev/driver")")
    fi
    runtime="unknown"
    if [ -r "$dev/power/runtime_status" ]; then
        IFS= read -r runtime < "$dev/power/runtime_status"
    fi

    revision="unknown"
    access="sysfs-visible"
    if [ "$lspci_available" -eq 1 ]; then
        pci_line=$(printf '%s\n' "$lspci_output" | awk -v slot="$bdf" '$1 == slot { print; exit }')
        if [ -z "$pci_line" ]; then
            access="unreachable"
        else
            parsed_revision=$(printf '%s\n' "$pci_line" | sed -n 's/.*(rev \([[:xdigit:]][[:xdigit:]]\)).*/\1/p')
            if [ -n "$parsed_revision" ]; then
                revision=$parsed_revision
            fi
            if [ "$revision" = "ff" ] || [ "$revision" = "FF" ]; then
                access="unreachable"
            else
                access="pci-visible"
            fi
        fi
    fi

    printf 'GPU\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$bdf" "$device_id" "$driver" "$runtime" "$revision" "$access"
done

for gpu_dir in /proc/driver/nvidia/gpus/*; do
    [ -d "$gpu_dir" ] || continue
    printf 'KNOWN\t%s\n' "${gpu_dir##*/}"
done

(dmesg 2>/dev/null || true) \
    | grep -Ei 'NVRM|Xid|fallen off' \
    | tail -n 20 \
    | while IFS= read -r line; do printf 'KERNEL\t%s\n' "$line"; done
"""


app = Flask(__name__)
app.logger.setLevel(logging.INFO)
db_lock = threading.Lock()
start_lock = threading.Lock()
polling_lock = threading.Lock()
polling_started = False
config_watcher_started = False
db_initialized = False
collector_lock_fd: int | None = None
collector_role_decided = False
is_collector_process = False
stop_event = threading.Event()
polling_threads: dict[str, tuple[str, threading.Event, threading.Thread]] = {}
ansi_converter = Ansi2HTMLConverter(inline=True)
last_prune_at_epoch = 0.0

TERMINFO_PADDING_RE = re.compile(r"\$<\d+>")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")
BLACK_FOREGROUND_RE = re.compile(
    r"color\s*:\s*(?:#000(?:000)?|black)\s*;?", re.IGNORECASE
)
ANSI_CLEAR_RE = re.compile(r"\x1b\[[0-9;?]*[HfJ]")
WATCH_FRAME_SPLIT_RE = re.compile(
    r"\x1b\[[0-9;?]*2J\x1b\[[0-9;?]*H|\x1b\[[0-9;?]*H\x1b\[[0-9;?]*J"
)
# DCS sequences such as XTGETTCAP are terminal capability queries, not gpustat output.
# Remove the complete sequence before stripping individual ESC controls so its payload
# (for example, "+q544e...") cannot leak into the rendered terminal text.
ANSI_DCS_RE = re.compile(r"\x1bP.*?(?:\x1b\\|\x9c)", re.DOTALL)
ANSI_NON_SGR_RE = re.compile(r"\x1b\[(?![0-9;]*m)[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
ANSI_DEC_CURSOR_RE = re.compile(r"\x1b[78]")
GPUSTAT_HEADER_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\b"
)

for logger_name in ("paramiko", "paramiko.transport", "paramiko.client"):
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False


def load_servers(servers_file: Path) -> list[dict]:
    with servers_file.open("r", encoding="utf-8") as f:
        servers = json.load(f)

    if not isinstance(servers, list):
        raise ValueError("servers.json 必须是服务器对象数组")

    server_names: set[str] = set()
    server_ips: set[str] = set()
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            raise ValueError(f"servers.json 第 {index + 1} 项必须是服务器对象")

        name = str(server.get("name", "")).strip()
        ip = str(server.get("ip", "")).strip()
        username = str(server.get("username", "")).strip()
        if not name or not ip or not username:
            raise ValueError(
                f"servers.json 第 {index + 1} 项必须包含 name、ip 和 username"
            )
        if name in server_names:
            raise ValueError(f"servers.json 中存在重复的服务器名称: {name}")
        if ip in server_ips:
            raise ValueError(f"servers.json 中存在重复的服务器地址: {ip}")
        if (
            "env" in server
            and server["env"] is not None
            and not isinstance(server["env"], dict)
        ):
            raise ValueError(f"服务器 {name} 的 env 必须是对象")
        server_names.add(name)
        server_ips.add(ip)

    return servers


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                ip TEXT NOT NULL,
                username TEXT NOT NULL,
                command TEXT NOT NULL,
                stdout TEXT NOT NULL DEFAULT '',
                stdout_html TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '',
                exit_code INTEGER,
                status TEXT NOT NULL,
                connection_status TEXT NOT NULL DEFAULT 'unknown',
                last_success_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL
            )
            """)
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(server_readings)").fetchall()
        }
        if "connection_status" not in existing_columns:
            conn.execute(
                "ALTER TABLE server_readings ADD COLUMN connection_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "stdout_html" not in existing_columns:
            conn.execute(
                "ALTER TABLE server_readings ADD COLUMN stdout_html TEXT NOT NULL DEFAULT ''"
            )
        if "last_success_at" not in existing_columns:
            conn.execute(
                "ALTER TABLE server_readings ADD COLUMN last_success_at TEXT NOT NULL DEFAULT ''"
            )
            conn.execute("""
                UPDATE server_readings
                SET last_success_at = fetched_at
                WHERE status = 'ok' AND connection_status = 'connected'
                """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_server_readings_fetched_at
            ON server_readings (fetched_at)
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_server_readings_ip_id
            ON server_readings (ip, id DESC)
            """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_json_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                ip TEXT NOT NULL,
                username TEXT NOT NULL,
                command TEXT NOT NULL,
                stdout_json TEXT NOT NULL DEFAULT '',
                exit_code INTEGER,
                status TEXT NOT NULL,
                connection_status TEXT NOT NULL DEFAULT 'unknown',
                last_success_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL
            )
            """)
        existing_json_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(server_json_readings)"
            ).fetchall()
        }
        if "connection_status" not in existing_json_columns:
            conn.execute(
                "ALTER TABLE server_json_readings ADD COLUMN connection_status TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "last_success_at" not in existing_json_columns:
            conn.execute(
                "ALTER TABLE server_json_readings ADD COLUMN last_success_at TEXT NOT NULL DEFAULT ''"
            )
            conn.execute("""
                UPDATE server_json_readings
                SET last_success_at = fetched_at
                WHERE status = 'ok' AND connection_status = 'connected'
                """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_server_json_readings_fetched_at
            ON server_json_readings (fetched_at)
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_server_json_readings_ip_id
            ON server_json_readings (ip, id DESC)
            """)

        # Connection-session timestamps are kept separately from short-lived
        # readings, so retention cleanup cannot remove them.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_connection_state (
                ip TEXT PRIMARY KEY,
                server_name TEXT NOT NULL,
                connection_status TEXT NOT NULL DEFAULT 'unknown',
                online_since_at TEXT NOT NULL DEFAULT '',
                last_online_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """)
        conn.execute("""
            INSERT OR IGNORE INTO server_connection_state
            (ip, server_name, connection_status, online_since_at, last_online_at, updated_at)
            SELECT sr.ip,
                   sr.server_name,
                   'unknown',
                   '',
                   CASE
                       WHEN sr.last_success_at != '' THEN sr.last_success_at
                       WHEN sr.connection_status = 'connected' THEN sr.fetched_at
                       ELSE ''
                   END,
                   sr.fetched_at
            FROM server_readings sr
            JOIN (
                SELECT ip, MAX(id) AS max_id
                FROM server_readings
                GROUP BY ip
            ) latest ON sr.id = latest.max_id
            """)
        conn.commit()


def prune_old_readings(conn: sqlite3.Connection, fetched_at: str) -> None:
    cutoff = (
        datetime.fromisoformat(fetched_at) - timedelta(minutes=RETENTION_MINUTES)
    ).isoformat()
    cursor = conn.execute(
        "DELETE FROM server_readings WHERE fetched_at < ?",
        (cutoff,),
    )
    if cursor.rowcount > 0:
        app.logger.info("已清理 server_readings 过期数据 %s 条", cursor.rowcount)


def prune_old_json_readings(conn: sqlite3.Connection, fetched_at: str) -> None:
    cutoff = (
        datetime.fromisoformat(fetched_at) - timedelta(minutes=RETENTION_MINUTES)
    ).isoformat()
    cursor = conn.execute(
        "DELETE FROM server_json_readings WHERE fetched_at < ?",
        (cutoff,),
    )
    if cursor.rowcount > 0:
        app.logger.info("已清理 server_json_readings 过期数据 %s 条", cursor.rowcount)


def ansi_to_html_fragment(text: str) -> str:
    html = ansi_converter.convert(text, full=False)
    return BLACK_FOREGROUND_RE.sub("color: #ffffff;", html)


def normalize_stream_text(stream_text: str) -> str:
    # Keep only latest gpustat snapshot: remove cursor controls, then cut from the last header line.
    tail = stream_text[-120000:]
    cleaned = ANSI_DCS_RE.sub("", tail)
    cleaned = ANSI_CLEAR_RE.sub("", cleaned)
    cleaned = ANSI_NON_SGR_RE.sub("", cleaned)
    cleaned = ANSI_DEC_CURSOR_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r", "")

    lines = cleaned.split("\n")
    latest_header_index = -1
    for idx, line in enumerate(lines):
        if GPUSTAT_HEADER_RE.search(line):
            latest_header_index = idx

    if latest_header_index >= 0:
        frame_lines = lines[latest_header_index:]
    else:
        frame_lines = lines

    # Drop leading/trailing empties after slicing.
    while frame_lines and not frame_lines[0].strip():
        frame_lines.pop(0)
    while frame_lines and not frame_lines[-1].strip():
        frame_lines.pop()

    return "\n".join(frame_lines)


def connect_ssh_client(server: dict) -> paramiko.SSHClient:
    ip = server.get("ip")
    username = server.get("username")
    if not ip or not username:
        raise ValueError("missing ip or username")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ip,
        username=username,
        key_filename=str(SSH_KEY_FILE),
        timeout=SSH_TIMEOUT_SECONDS,
        banner_timeout=SSH_TIMEOUT_SECONDS,
        auth_timeout=SSH_TIMEOUT_SECONDS,
    )
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return client


def command_with_server_env(server: dict, command: str) -> str:
    """Prefix a remote command with the environment configured for one server."""
    environment = server.get("env", {})
    if environment is None:
        environment = {}
    if not isinstance(environment, dict):
        raise ValueError("server env must be an object")

    assignments = []
    for name, value in environment.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", name
        ):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if value is None:
            raise ValueError(f"environment variable {name} must not be null")
        assignments.append(f"{name}={shlex.quote(str(value))}")

    return " ".join([*assignments, command])


def maybe_prune(conn: sqlite3.Connection, fetched_at: str) -> None:
    global last_prune_at_epoch
    now_epoch = datetime.now(timezone.utc).timestamp()
    if now_epoch - last_prune_at_epoch < PRUNE_INTERVAL_SECONDS:
        return

    prune_old_readings(conn, fetched_at)
    prune_old_json_readings(conn, fetched_at)
    last_prune_at_epoch = now_epoch


def save_poll_result(
    server: dict,
    command: str,
    json_command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    status: str,
    connection_status: str,
    fetched_at: str,
    last_success_at: str | None,
    json_stdout: str,
    json_exit_code: int | None,
    json_status: str,
    json_last_success_at: str | None,
) -> None:
    stdout_html = ansi_to_html_fragment(stdout)

    with db_lock:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_readings
                (server_name, ip, username, command, stdout, stdout_html, stderr, exit_code, status, connection_status, last_success_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server.get("name", "unknown"),
                    server.get("ip", ""),
                    server.get("username", ""),
                    command,
                    stdout,
                    stdout_html,
                    stderr,
                    exit_code,
                    status,
                    connection_status,
                    last_success_at or "",
                    fetched_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO server_json_readings
                (server_name, ip, username, command, stdout_json, exit_code, status, connection_status, last_success_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server.get("name", "unknown"),
                    server.get("ip", ""),
                    server.get("username", ""),
                    json_command,
                    json_stdout,
                    json_exit_code,
                    json_status,
                    connection_status,
                    json_last_success_at or "",
                    fetched_at,
                ),
            )
            update_server_connection_state(
                conn=conn,
                server=server,
                connection_status=connection_status,
                checked_at=fetched_at,
            )
            maybe_prune(conn, fetched_at)
            conn.commit()


def fetch_latest_readings() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT sr.server_name, sr.ip, sr.username, sr.command, sr.stdout, sr.stderr,
                     sr.stdout_html, sr.exit_code, sr.status, sr.connection_status, sr.last_success_at, sr.fetched_at
            FROM server_readings sr
            JOIN (
                SELECT ip, MAX(id) AS max_id
                FROM server_readings
                GROUP BY ip
            ) latest ON sr.id = latest.max_id
            ORDER BY sr.server_name
            """).fetchall()

    return [dict(row) for row in rows]


def update_server_connection_state(
    conn: sqlite3.Connection,
    server: dict,
    connection_status: str,
    checked_at: str,
) -> None:
    """Persist the current connection session without relying on retained readings."""
    ip = str(server.get("ip", ""))
    server_name = str(server.get("name", "unknown"))
    existing = conn.execute(
        """
        SELECT connection_status, online_since_at, last_online_at
        FROM server_connection_state
        WHERE ip = ?
        """,
        (ip,),
    ).fetchone()

    if connection_status == "connected":
        is_continuing_session = (
            existing is not None
            and existing["connection_status"] == "connected"
            and bool(existing["online_since_at"])
        )
        online_since_at = existing["online_since_at"] if is_continuing_session else checked_at
        conn.execute(
            """
            INSERT INTO server_connection_state
            (ip, server_name, connection_status, online_since_at, last_online_at, updated_at)
            VALUES (?, ?, 'connected', ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                server_name = excluded.server_name,
                connection_status = excluded.connection_status,
                online_since_at = excluded.online_since_at,
                last_online_at = excluded.last_online_at,
                updated_at = excluded.updated_at
            """,
            (ip, server_name, online_since_at, checked_at, checked_at),
        )
        return

    if existing is None:
        conn.execute(
            """
            INSERT INTO server_connection_state
            (ip, server_name, connection_status, online_since_at, last_online_at, updated_at)
            VALUES (?, ?, ?, '', '', ?)
            """,
            (ip, server_name, connection_status, checked_at),
        )
        return

    conn.execute(
        """
        UPDATE server_connection_state
        SET server_name = ?, connection_status = ?, updated_at = ?
        WHERE ip = ?
        """,
        (server_name, connection_status, checked_at, ip),
    )


def fetch_connection_states() -> dict[str, dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT ip, connection_status, online_since_at, last_online_at
            FROM server_connection_state
            """
        ).fetchall()
    return {str(row["ip"]): dict(row) for row in rows}


def fetch_latest_json_readings() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT sr.server_name, sr.ip, sr.username, sr.command, sr.stdout_json,
                   sr.exit_code, sr.status, sr.connection_status, sr.last_success_at, sr.fetched_at
            FROM server_json_readings sr
            JOIN (
                SELECT ip, MAX(id) AS max_id
                FROM server_json_readings
                GROUP BY ip
            ) latest ON sr.id = latest.max_id
            ORDER BY sr.server_name
            """).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        payload_text = item.get("stdout_json") or ""
        if payload_text:
            try:
                item["json_data"] = json.loads(payload_text)
            except json.JSONDecodeError:
                item["json_data"] = None
        else:
            item["json_data"] = None
        item.pop("stdout_json", None)
        results.append(item)

    return results


def merge_configured_servers(
    readings: list[dict], include_json_data: bool = False
) -> list[dict]:
    """Return configured servers in servers.json order, enriched with their latest reading."""
    readings_by_ip = {str(item.get("ip", "")): item for item in readings}
    connection_states_by_ip = fetch_connection_states()
    results = []
    for server in load_servers(SERVERS_FILE):
        ip = str(server.get("ip", ""))
        item = dict(readings_by_ip.get(ip, {}))
        connection_state = connection_states_by_ip.get(ip, {})
        item.update(
            {
                "server_name": server.get("name", "unknown"),
                "ip": ip,
                "username": server.get("username", ""),
                "online_since_at": connection_state.get("online_since_at", ""),
                "last_online_at": connection_state.get("last_online_at", ""),
            }
        )
        item.setdefault("command", "")
        item.setdefault("exit_code", None)
        item.setdefault("status", "pending")
        item.setdefault("connection_status", "unknown")
        item.setdefault("last_success_at", "")
        item.setdefault("fetched_at", "")
        if include_json_data:
            item.setdefault("json_data", None)
        else:
            item.setdefault("stdout", "")
            item.setdefault("stdout_html", "")
            item.setdefault("stderr", "")
        results.append(item)

    return results


def fetch_last_success_at(table_name: str, ip: str) -> str | None:
    if table_name not in {"server_readings", "server_json_readings"}:
        raise ValueError(f"unsupported table: {table_name}")

    with get_db_connection() as conn:
        row = conn.execute(
            f"""
            SELECT last_success_at
            FROM {table_name}
            WHERE ip = ? AND last_success_at != ''
            ORDER BY id DESC
            LIMIT 1
            """,
            (ip,),
        ).fetchone()
    return row["last_success_at"] if row is not None else None


def fetch_latest_collector_state(ip: str) -> dict:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT status, stdout, stderr
            FROM server_readings
            WHERE ip = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (ip,),
        ).fetchone()
    return dict(row) if row is not None else {}


def load_server_name_to_ip_map() -> dict[str, str]:
    servers = load_servers(SERVERS_FILE)
    mapping: dict[str, str] = {}
    for server in servers:
        name = str(server.get("name", "")).strip()
        ip = str(server.get("ip", "")).strip()
        if name and ip:
            mapping[name] = ip
    return mapping


def start_watch_streams(client: paramiko.SSHClient, command: str):
    _, text_stdout, text_stderr = client.exec_command(
        command,
        get_pty=True,
        timeout=GPUSTAT_COMMAND_TIMEOUT_SECONDS,
    )
    return text_stdout, text_stderr


class RemoteCommandTimeout(TimeoutError):
    pass


class PollingStopped(Exception):
    pass


def run_remote_command_with_timeout(
    client: paramiko.SSHClient,
    command: str,
    timeout_seconds: float,
    server_stop_event: threading.Event,
) -> tuple[str, str, int]:
    """Run one SSH command without waiting forever for a wedged remote process."""
    _, stdout, _ = client.exec_command(
        command,
        get_pty=False,
        timeout=max(1.0, timeout_seconds),
    )
    channel = stdout.channel
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    deadline = time.monotonic() + timeout_seconds

    try:
        while True:
            while channel.recv_ready():
                stdout_chunks.append(channel.recv(65535))
            while channel.recv_stderr_ready():
                stderr_chunks.append(channel.recv_stderr(65535))

            if channel.exit_status_ready():
                exit_code = channel.recv_exit_status()
                while channel.recv_ready():
                    stdout_chunks.append(channel.recv(65535))
                while channel.recv_stderr_ready():
                    stderr_chunks.append(channel.recv_stderr(65535))
                return (
                    b"".join(stdout_chunks).decode("utf-8", errors="ignore"),
                    b"".join(stderr_chunks).decode("utf-8", errors="ignore"),
                    exit_code,
                )

            if stop_event.is_set() or server_stop_event.is_set():
                raise PollingStopped()
            if time.monotonic() >= deadline:
                raise RemoteCommandTimeout(
                    f"remote command timed out after {timeout_seconds:g}s"
                )
            time.sleep(0.05)
    finally:
        if not channel.exit_status_ready():
            channel.close()


def close_watch_streams(text_stdout, text_stderr) -> None:
    channels = []
    for stream in (text_stdout, text_stderr):
        channel = getattr(stream, "channel", None)
        if channel is not None and channel not in channels:
            channels.append(channel)
    for channel in channels:
        try:
            channel.close()
        except Exception:
            pass


def parse_pci_fallback_probe(output: str, known_gpu_bdfs: set[str]) -> dict:
    boot_id = ""
    devices_by_bdf: dict[str, dict] = {}
    kernel_messages: list[str] = []
    discovered_bdfs = set(known_gpu_bdfs)

    for raw_line in output.splitlines():
        kind, separator, payload = raw_line.partition("\t")
        if not separator:
            continue
        if kind == "BOOT":
            boot_id = payload.strip()
            continue
        if kind == "KNOWN":
            bdf = payload.strip().lower()
            if bdf:
                discovered_bdfs.add(bdf)
            continue
        if kind == "KERNEL":
            message = payload.strip()
            if message:
                kernel_messages.append(message)
            continue
        if kind != "GPU":
            continue

        parts = payload.split("\t")
        if len(parts) != 6:
            continue
        bdf, device_id, driver, runtime, revision, access = parts
        bdf = bdf.strip().lower()
        if not bdf:
            continue
        discovered_bdfs.add(bdf)
        devices_by_bdf[bdf] = {
            "bdf": bdf,
            "device_id": device_id.strip(),
            "driver": driver.strip() or "unbound",
            "runtime": runtime.strip() or "unknown",
            "revision": revision.strip() or "unknown",
            "access": access.strip() or "unknown",
        }

    for bdf in discovered_bdfs:
        devices_by_bdf.setdefault(
            bdf,
            {
                "bdf": bdf,
                "device_id": "unknown",
                "driver": "missing",
                "runtime": "unknown",
                "revision": "unknown",
                "access": "missing",
            },
        )

    for device in devices_by_bdf.values():
        bdf = device["bdf"]
        has_kernel_error = any(
            bdf in message.lower()
            and re.search(r"NVRM|Xid|fallen off|rm_init_adapter", message, re.I)
            for message in kernel_messages
        )
        if device["access"] == "missing":
            state = "missing"
        elif device["access"] == "unreachable" or device["revision"].lower() == "ff":
            state = "pci_unreachable"
        elif device["driver"] != "nvidia":
            state = "driver_unbound"
        elif has_kernel_error:
            state = "driver_error"
        else:
            state = "pci_present"
        device["state"] = state

    devices = sorted(devices_by_bdf.values(), key=lambda item: item["bdf"])
    return {
        "boot_id": boot_id,
        "devices": devices,
        "known_gpu_bdfs": discovered_bdfs,
        "kernel_messages": kernel_messages,
        "all_ready": bool(devices)
        and all(device["state"] == "pci_present" for device in devices),
        # Historical kernel messages can keep a device in driver_error even
        # after it becomes usable again. It is safe to make a rate-limited
        # gpustat recovery attempt once every device is PCI-visible and bound.
        "recovery_ready": bool(devices)
        and all(
            device["state"] in {"pci_present", "driver_error"}
            for device in devices
        ),
    }


def format_pci_fallback_text(
    probe: dict,
    reason: str,
    checked_at: str,
    last_success_at: str | None = None,
) -> str:
    ansi_red = "\x1b[91m"
    ansi_green = "\x1b[92m"
    ansi_reset = "\x1b[0m"
    state_labels = {
        "pci_present": "PCI_PRESENT",
        "driver_unbound": "DRIVER_UNBOUND",
        "pci_unreachable": "PCI_UNREACHABLE",
        "driver_error": "DRIVER_ERROR",
        "missing": "MISSING",
    }
    lines = [
        f"{ansi_red}GPUSTAT DEGRADED MODE{ansi_reset}",
        f"{ansi_red}Reason:{ansi_reset} {reason}",
        f"Checked at: {checked_at}",
        (
            f"{ansi_green}Last healthy at:{ansi_reset} "
            f"{last_success_at or 'N/A'}"
        ),
        f"Boot ID: {probe.get('boot_id') or 'unknown'}",
        "Source: PCI sysfs + lspci + kernel log (no NVML)",
        "",
        "NVIDIA display/3D devices:",
        "BDF             DEVICE       DRIVER          REV  RUNTIME     STATE",
    ]

    devices = probe.get("devices") or []
    if not devices:
        lines.append(
            f"{ansi_red}No NVIDIA display/3D PCI device was found.{ansi_reset}"
        )
    for device in devices:
        device_id = str(device.get("device_id", "unknown")).removeprefix("0x")
        driver = str(device.get("driver", "unknown"))
        revision = str(device.get("revision", "unknown"))
        runtime = str(device.get("runtime", "unknown"))
        state = state_labels.get(str(device.get("state")), "UNKNOWN")
        state_color = ansi_green if state == "PCI_PRESENT" else ansi_red
        lines.append(
            f"{str(device.get('bdf', 'unknown')):<15} "
            f"10de:{device_id:<6} "
            f"{driver:<15} "
            f"{revision:<4} "
            f"{runtime:<11} "
            f"{state_color}{state}{ansi_reset}"
        )

    kernel_messages = probe.get("kernel_messages") or []
    if kernel_messages:
        lines.extend(
            ["", f"{ansi_red}Recent NVIDIA kernel messages:{ansi_reset}"]
        )
        lines.extend(
            f"{ansi_red}{message}{ansi_reset}"
            for message in kernel_messages[-8:]
        )

    lines.extend(
        [
            "",
            "The backend keeps this lightweight probe active and will restore",
            "the normal gpustat console automatically after a successful retry.",
        ]
    )
    return "\n".join(lines)


def probe_pci_fallback(
    client: paramiko.SSHClient,
    server_stop_event: threading.Event,
    known_gpu_bdfs: set[str],
) -> dict:
    output, stderr, exit_code = run_remote_command_with_timeout(
        client,
        PCI_FALLBACK_COMMAND,
        FALLBACK_COMMAND_TIMEOUT_SECONDS,
        server_stop_event,
    )
    if exit_code != 0 and not output:
        raise RuntimeError(
            f"PCI fallback probe failed with exit code {exit_code}: {stderr.strip()}"
        )
    return parse_pci_fallback_probe(output, known_gpu_bdfs)


def fetch_gpustat_json_bounded(
    client: paramiko.SSHClient,
    json_command: str,
    server_stop_event: threading.Event,
) -> tuple[str, str, int]:
    stdout, stderr, exit_code = run_remote_command_with_timeout(
        client,
        json_command,
        GPUSTAT_COMMAND_TIMEOUT_SECONDS,
        server_stop_event,
    )
    payload = stdout.strip()
    if exit_code != 0:
        raise RuntimeError(
            f"gpustat --json exited with code {exit_code}: {stderr.strip()}"
        )
    if not payload:
        raise RuntimeError("gpustat --json returned no data")
    try:
        json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gpustat --json returned invalid JSON: {exc}") from exc
    return payload, stderr.strip(), exit_code


def poll_server_forever(server: dict, server_stop_event: threading.Event) -> None:
    watch_command = command_with_server_env(server, COMMAND)
    json_command = command_with_server_env(server, COMMAND_JSON)
    server_ip = str(server.get("ip", ""))
    previous_state = fetch_latest_collector_state(server_ip)
    previous_stdout = str(previous_state.get("stdout") or "")
    previous_boot_id_match = re.search(
        r"^Boot ID:\s*(\S+)",
        previous_stdout,
        re.MULTILINE,
    )
    client = None
    text_stdout = None
    text_stderr = None
    text_stream = ""
    text_stderr_stream = ""
    latest_json_text = ""
    latest_json_exit_code: int | None = None
    latest_json_status = "error"
    last_json_polled_at = 0.0
    save_interval = max(0.3, POLL_INTERVAL_SECONDS * 0.5)
    last_saved_at = 0.0
    last_watch_output_at = 0.0
    degraded = previous_state.get("status") == "degraded"
    degraded_reason = (
        str(previous_state.get("stderr") or "continuing previous degraded state")
        if degraded
        else ""
    )
    degraded_boot_id = (
        previous_boot_id_match.group(1)
        if degraded and previous_boot_id_match is not None
        else ""
    )
    known_gpu_bdfs: set[str] = (
        {
            bdf.lower()
            for bdf in re.findall(
                r"(?m)^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])\s",
                previous_stdout,
            )
        }
        if degraded
        else set()
    )
    last_fallback_polled_at = 0.0
    last_fallback_recovery_ready: bool | None = None
    last_recovery_attempt_at = time.monotonic() if degraded else 0.0
    last_success_at = fetch_last_success_at("server_readings", server_ip)
    last_json_success_at = fetch_last_success_at(
        "server_json_readings", server_ip
    )

    while not stop_event.is_set() and not server_stop_event.is_set():
        try:
            transport = client.get_transport() if client is not None else None
            if client is None or transport is None or not transport.is_active():
                client = connect_ssh_client(server)
                text_stdout = None
                text_stderr = None
                text_stream = ""
                text_stderr_stream = ""
                latest_json_text = ""
                latest_json_exit_code = None
                latest_json_status = "error"
                last_saved_at = 0.0
                last_json_polled_at = 0.0
                last_watch_output_at = 0.0
                last_fallback_polled_at = 0.0

            now = time.monotonic()

            if degraded:
                if (
                    now - last_fallback_polled_at
                    < FALLBACK_POLL_INTERVAL_SECONDS
                ):
                    if server_stop_event.wait(0.2) or stop_event.is_set():
                        break
                    continue

                fetched_at = utc_now_iso()
                try:
                    probe = probe_pci_fallback(
                        client,
                        server_stop_event,
                        known_gpu_bdfs,
                    )
                except PollingStopped:
                    break
                except Exception as exc:
                    transport = client.get_transport()
                    if transport is None or not transport.is_active():
                        raise
                    probe_error = f"PCI fallback probe failed: {exc}"
                    fallback_text = "\n".join(
                        [
                            "GPUSTAT DEGRADED MODE",
                            f"Reason: {degraded_reason}",
                            f"Checked at: {fetched_at}",
                            f"Last healthy at: {last_success_at or 'N/A'}",
                            "Source: PCI fallback probe",
                            "",
                            probe_error,
                            "The backend will retry the lightweight probe.",
                        ]
                    )
                    save_poll_result(
                        server=server,
                        command="PCI/sysfs fallback probe",
                        json_command=json_command,
                        stdout=fallback_text,
                        stderr=probe_error,
                        exit_code=None,
                        status="degraded",
                        connection_status="connected",
                        fetched_at=fetched_at,
                        last_success_at=last_success_at,
                        json_stdout="",
                        json_exit_code=None,
                        json_status="degraded",
                        json_last_success_at=last_json_success_at,
                    )
                    last_fallback_polled_at = time.monotonic()
                    if server_stop_event.wait(0.2) or stop_event.is_set():
                        break
                    continue

                known_gpu_bdfs = set(probe["known_gpu_bdfs"])
                fallback_text = format_pci_fallback_text(
                    probe,
                    degraded_reason,
                    fetched_at,
                    last_success_at,
                )
                save_poll_result(
                    server=server,
                    command="PCI/sysfs fallback probe",
                    json_command=json_command,
                    stdout=fallback_text,
                    stderr=degraded_reason,
                    exit_code=None,
                    status="degraded",
                    connection_status="connected",
                    fetched_at=fetched_at,
                    last_success_at=last_success_at,
                    json_stdout="",
                    json_exit_code=None,
                    json_status="degraded",
                    json_last_success_at=last_json_success_at,
                )

                probe_boot_id = str(probe.get("boot_id") or "")
                boot_changed = bool(
                    degraded_boot_id
                    and probe_boot_id
                    and degraded_boot_id != probe_boot_id
                )
                became_ready = (
                    last_fallback_recovery_ready is False
                    and probe["recovery_ready"]
                )
                periodic_retry_due = (
                    now - last_recovery_attempt_at
                    >= GPUSTAT_RECOVERY_PROBE_SECONDS
                )
                should_try_recovery = probe["recovery_ready"] and (
                    boot_changed or became_ready or periodic_retry_due
                )
                if probe_boot_id:
                    degraded_boot_id = probe_boot_id
                last_fallback_recovery_ready = probe["recovery_ready"]
                last_fallback_polled_at = time.monotonic()

                if should_try_recovery:
                    last_recovery_attempt_at = time.monotonic()
                    try:
                        (
                            latest_json_text,
                            json_err,
                            latest_json_exit_code,
                        ) = fetch_gpustat_json_bounded(
                            client,
                            json_command,
                            server_stop_event,
                        )
                    except PollingStopped:
                        break
                    except Exception as exc:
                        degraded_reason = f"gpustat recovery probe failed: {exc}"[
                            :500
                        ]
                        app.logger.warning(
                            "服务器 %s 的 gpustat 恢复探测失败: %s",
                            server.get("name", "unknown"),
                            exc,
                        )
                    else:
                        latest_json_status = "ok"
                        last_json_success_at = utc_now_iso()
                        last_json_polled_at = time.monotonic()
                        if json_err:
                            text_stderr_stream = json_err[-20000:]
                        text_stdout, text_stderr = start_watch_streams(
                            client,
                            watch_command,
                        )
                        text_stream = ""
                        last_watch_output_at = time.monotonic()
                        last_saved_at = 0.0
                        degraded = False
                        degraded_reason = ""
                        degraded_boot_id = probe_boot_id
                        last_fallback_recovery_ready = None
                        app.logger.info(
                            "服务器 %s 的 gpustat 已恢复，切回正常采集",
                            server.get("name", "unknown"),
                        )

                if server_stop_event.wait(0.2) or stop_event.is_set():
                    break
                continue

            has_update = False

            if text_stdout is None:
                try:
                    (
                        latest_json_text,
                        json_err,
                        latest_json_exit_code,
                    ) = fetch_gpustat_json_bounded(
                        client,
                        json_command,
                        server_stop_event,
                    )
                except PollingStopped:
                    break
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"gpustat initial probe failed: {exc}"[:500]
                    last_recovery_attempt_at = time.monotonic()
                    last_fallback_polled_at = 0.0
                    last_fallback_recovery_ready = None
                    close_watch_streams(text_stdout, text_stderr)
                    text_stdout = None
                    text_stderr = None
                    app.logger.warning(
                        "服务器 %s 进入 PCI 降级采集: %s",
                        server.get("name", "unknown"),
                        exc,
                    )
                    continue

                latest_json_status = "ok"
                last_json_success_at = utc_now_iso()
                last_json_polled_at = time.monotonic()
                if json_err:
                    text_stderr_stream = (text_stderr_stream + "\n" + json_err)[
                        -20000:
                    ]
                text_stdout, text_stderr = start_watch_streams(client, watch_command)
                last_watch_output_at = time.monotonic()

            if text_stdout is not None and text_stdout.channel.recv_ready():
                chunk = text_stdout.channel.recv(65535).decode("utf-8", errors="ignore")
                if chunk:
                    text_stream += chunk
                    last_watch_output_at = time.monotonic()
                    has_update = True

            if text_stderr is not None and text_stderr.channel.recv_stderr_ready():
                err_chunk = text_stderr.channel.recv_stderr(65535).decode(
                    "utf-8", errors="ignore"
                )
                if err_chunk:
                    text_stderr_stream = (text_stderr_stream + err_chunk)[-20000:]

            now = time.monotonic()
            normalized_text = normalize_stream_text(text_stream)
            watch_exited = (
                text_stdout is not None
                and text_stdout.channel.exit_status_ready()
            )
            watch_timed_out = (
                text_stdout is not None
                and now - last_watch_output_at >= GPUSTAT_COMMAND_TIMEOUT_SECONDS
            )
            if watch_exited or watch_timed_out:
                degraded = True
                if watch_exited:
                    degraded_reason = "gpustat watch exited before producing a usable frame"
                elif not normalized_text:
                    degraded_reason = (
                        "gpustat watch produced no frame within "
                        f"{GPUSTAT_COMMAND_TIMEOUT_SECONDS:g}s"
                    )
                else:
                    degraded_reason = (
                        "gpustat watch stopped updating for "
                        f"{GPUSTAT_COMMAND_TIMEOUT_SECONDS:g}s"
                    )
                last_recovery_attempt_at = now
                last_fallback_polled_at = 0.0
                last_fallback_recovery_ready = None
                close_watch_streams(text_stdout, text_stderr)
                text_stdout = None
                text_stderr = None
                app.logger.warning(
                    "服务器 %s 进入 PCI 降级采集: %s",
                    server.get("name", "unknown"),
                    degraded_reason,
                )
                continue

            if now - last_json_polled_at >= POLL_INTERVAL_SECONDS:
                try:
                    (
                        latest_json_text,
                        json_err,
                        latest_json_exit_code,
                    ) = fetch_gpustat_json_bounded(
                        client,
                        json_command,
                        server_stop_event,
                    )
                    if json_err:
                        text_stderr_stream = (text_stderr_stream + "\n" + json_err)[
                            -20000:
                        ]
                    latest_json_status = "ok"
                    last_json_success_at = utc_now_iso()
                except PollingStopped:
                    break
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"gpustat --json failed: {exc}"[:500]
                    last_recovery_attempt_at = now
                    last_fallback_polled_at = 0.0
                    last_fallback_recovery_ready = None
                    close_watch_streams(text_stdout, text_stderr)
                    text_stdout = None
                    text_stderr = None
                    latest_json_text = ""
                    latest_json_exit_code = None
                    latest_json_status = "degraded"
                    app.logger.warning(
                        "服务器 %s 进入 PCI 降级采集: %s",
                        server.get("name", "unknown"),
                        exc,
                    )
                    continue
                last_json_polled_at = now
                has_update = True

            if (
                has_update
                and normalized_text
                and now - last_saved_at >= save_interval
            ):
                fetched_at = utc_now_iso()
                transport = client.get_transport()
                connection_status = (
                    "connected"
                    if transport is not None and transport.is_active()
                    else "disconnected"
                )

                status = "ok"
                if status == "ok" and connection_status == "connected":
                    last_success_at = fetched_at
                if latest_json_status == "ok" and connection_status == "connected":
                    last_json_success_at = fetched_at

                save_poll_result(
                    server=server,
                    command=watch_command,
                    json_command=json_command,
                    stdout=normalized_text,
                    stderr=text_stderr_stream,
                    exit_code=0 if status == "ok" else None,
                    status=status,
                    connection_status=connection_status,
                    fetched_at=fetched_at,
                    last_success_at=last_success_at,
                    json_stdout=latest_json_text,
                    json_exit_code=latest_json_exit_code,
                    json_status=latest_json_status,
                    json_last_success_at=last_json_success_at,
                )
                last_saved_at = now

            if server_stop_event.wait(0.2) or stop_event.is_set():
                break

        except PollingStopped:
            break
        except Exception as exc:
            fetched_at = utc_now_iso()
            save_poll_result(
                server=server,
                command=watch_command,
                json_command=json_command,
                stdout="",
                stderr=str(exc)[:20000],
                exit_code=None,
                status="error",
                connection_status="disconnected",
                fetched_at=fetched_at,
                last_success_at=last_success_at,
                json_stdout="",
                json_exit_code=None,
                json_status="error",
                json_last_success_at=last_json_success_at,
            )
            close_watch_streams(text_stdout, text_stderr)
            if client is not None:
                client.close()
                client = None
            text_stdout = None
            text_stderr = None
            if server_stop_event.wait(POLL_INTERVAL_SECONDS) or stop_event.is_set():
                break

    if client is not None:
        close_watch_streams(text_stdout, text_stderr)
        client.close()


def server_config_signature(server: dict) -> str:
    return json.dumps(
        {
            "name": server.get("name"),
            "ip": server.get("ip"),
            "username": server.get("username"),
            "env": server.get("env", {}),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def sync_polling_threads() -> None:
    """Start, stop, or replace collectors to match the current servers.json."""
    servers = load_servers(SERVERS_FILE)
    desired_servers = {str(server["name"]): server for server in servers}
    desired_signatures = {
        name: server_config_signature(server)
        for name, server in desired_servers.items()
    }
    threads_to_start: list[threading.Thread] = []

    with polling_lock:
        for name, (signature, server_stop_event, _) in list(polling_threads.items()):
            if desired_signatures.get(name) != signature:
                server_stop_event.set()
                polling_threads.pop(name)
                app.logger.info("已停止服务器采集: %s", name)

        for name, server in desired_servers.items():
            if name in polling_threads:
                continue
            server_stop_event = threading.Event()
            thread = threading.Thread(
                target=poll_server_forever,
                args=(server, server_stop_event),
                daemon=True,
                name=f"collector-{name}",
            )
            polling_threads[name] = (
                desired_signatures[name],
                server_stop_event,
                thread,
            )
            threads_to_start.append(thread)
            app.logger.info("已启动服务器采集: %s", name)

    for thread in threads_to_start:
        thread.start()


def watch_server_config_forever() -> None:
    while not stop_event.is_set():
        try:
            sync_polling_threads()
        except Exception:
            app.logger.exception("刷新 servers.json 失败，保留当前采集配置")

        if stop_event.wait(max(1, SERVER_CONFIG_REFRESH_SECONDS)):
            break


def start_polling_threads() -> None:
    global polling_started, config_watcher_started
    sync_polling_threads()
    polling_started = True

    if not config_watcher_started:
        thread = threading.Thread(target=watch_server_config_forever, daemon=True)
        thread.start()
        config_watcher_started = True


def try_become_collector() -> bool:
    global collector_lock_fd

    fd = os.open(COLLECTOR_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        collector_lock_fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        app.logger.info("当前进程已成为独立采集 worker，pid=%s", os.getpid())
        return True
    except BlockingIOError:
        os.close(fd)
        app.logger.info("当前进程为只读 worker，采集由其他进程执行")
        return False


def ensure_runtime_initialized() -> None:
    global db_initialized, collector_role_decided, is_collector_process

    with start_lock:
        if not db_initialized:
            init_db()
            db_initialized = True
        if not collector_role_decided:
            is_collector_process = try_become_collector()
            collector_role_decided = True
        if is_collector_process and not polling_started:
            start_polling_threads()


@app.before_request
def initialize_before_request() -> None:
    ensure_runtime_initialized()


@app.route("/info", methods=["GET"])
def info() -> Response:
    return jsonify(
        {
            "interval_seconds": POLL_INTERVAL_SECONDS,
            "data": merge_configured_servers(fetch_latest_readings()),
        }
    )


@app.route("/health", methods=["GET"])
def health() -> Response:
    return jsonify(
        {
            "status": "ok",
            "polling_started": polling_started,
            "is_collector_process": is_collector_process,
            "interval_seconds": POLL_INTERVAL_SECONDS,
            "configured_server_count": len(load_servers(SERVERS_FILE)),
            "active_collector_count": (
                len(polling_threads) if is_collector_process else 0
            ),
            "db_exists": DB_FILE.exists(),
        }
    )


@app.route("/changelog", methods=["GET"])
def changelog() -> Response:
    try:
        content = CHANGELOG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Response("CHANGELOG.md not found", status=404, mimetype="text/plain")

    return Response(content, mimetype="text/markdown")


@app.route("/jinfo", methods=["GET"])
def jinfo() -> Response:
    return jsonify(
        {
            "interval_seconds": POLL_INTERVAL_SECONDS,
            "data": merge_configured_servers(
                fetch_latest_json_readings(), include_json_data=True
            ),
        }
    )


@app.route("/set_gpu", methods=["GET"])
def set_gpu() -> Response:
    try:
        content = (BASE_DIR / "assets" / "set_gpu.py").read_text(encoding="utf-8")
    except Exception:
        return Response(
            "assets/set_gpu.py not found", status=404, mimetype="text/plain"
        )

    t1_value = request.args.get("t1")
    t2_value = request.args.get("t2")
    ex_values = request.args.getlist("ex")

    if len(ex_values) == 1 and "," in ex_values[0]:
        ex_values = [part.strip() for part in ex_values[0].split(",") if part.strip()]

    try:
        if t1_value is not None:
            t1_number = float(t1_value)
            content = re.sub(
                r"(^\s*IDLE_SCORE_THRESHOLD_D3kTs\s*=\s*)(.+?)(\s*#\s*sym:IDLE_SCORE_THRESHOLD\s*$)",
                rf"\g<1>{t1_number!r}\g<3>",
                content,
                flags=re.MULTILINE,
            )
        if t2_value is not None:
            t2_number = float(t2_value)
            content = re.sub(
                r"(^\s*BUSY_SCORE_THRESHOLD_E4mUy\s*=\s*)(.+?)(\s*#\s*sym:BUSY_SCORE_THRESHOLD\s*$)",
                rf"\g<1>{t2_number!r}\g<3>",
                content,
                flags=re.MULTILINE,
            )
        if ex_values:
            server_name_to_ip = load_server_name_to_ip_map()
            exclude_ips = [
                server_name_to_ip[name]
                for name in ex_values
                if name in server_name_to_ip
            ]
            content = re.sub(
                r"(^\s*EXCLUDE_SERVERS_F5rQa\s*=\s*)(.+?)(\s*#\s*sym:EXCLUDE_SERVERS\s*$)",
                rf"\g<1>{exclude_ips!r}\g<3>",
                content,
                flags=re.MULTILINE,
            )
    except ValueError:
        return Response(
            "t1 and t2 must be floating point numbers",
            status=400,
            mimetype="text/plain",
        )

    return Response(content, mimetype="text/plain")


@app.route("/auto_set_gpu_example", methods=["GET"])
def auto_set_gpu_example() -> Response:
    try:
        content = (BASE_DIR / "assets" / "auto_set_gpu_example.py").read_text(
            encoding="utf-8"
        )
    except Exception:
        return Response(
            "assets/auto_set_gpu_example.py not found",
            status=404,
            mimetype="text/plain",
        )

    return Response(content, mimetype="text/plain")


@app.route("/auto_set_gpu_oneline", methods=["GET"])
def auto_set_gpu_oneline() -> Response:
    try:
        content = (BASE_DIR / "assets" / "auto_set_gpu_oneline.py").read_text(
            encoding="utf-8"
        )
    except Exception:
        return Response(
            "assets/auto_set_gpu_oneline.py not found",
            status=404,
            mimetype="text/plain",
        )

    return Response(content, mimetype="text/plain")


@app.route("/", methods=["GET"])
def index() -> Response:
    return Response(
        (BASE_DIR / "index.html").read_text(encoding="utf-8"), mimetype="text/html"
    )


def main() -> None:
    ensure_runtime_initialized()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "2223")),
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
