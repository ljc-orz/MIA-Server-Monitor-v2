import json
import logging
import os
import re
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
SSH_KEY_FILE = BASE_DIR / "id_rsa_guangxing"
DB_FILE = BASE_DIR / "server_monitor.sqlite3"
COLLECTOR_LOCK_FILE = BASE_DIR / "collector.lock"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
COMMAND = f"TERM=xterm-256color gpustat -P --watch {POLL_INTERVAL_SECONDS}"
COMMAND_JSON = "gpustat --json"
SSH_TIMEOUT_SECONDS = int(os.getenv("SSH_TIMEOUT_SECONDS", "10"))
RETENTION_MINUTES = int(os.getenv("RETENTION_MINUTES", "10"))
PRUNE_INTERVAL_SECONDS = int(os.getenv("PRUNE_INTERVAL_SECONDS", "60"))


app = Flask(__name__)
app.logger.setLevel(logging.INFO)
db_lock = threading.Lock()
start_lock = threading.Lock()
polling_started = False
db_initialized = False
collector_lock_fd: int | None = None
collector_role_decided = False
is_collector_process = False
stop_event = threading.Event()
ansi_converter = Ansi2HTMLConverter(inline=True)
last_prune_at_epoch = 0.0

TERMINFO_PADDING_RE = re.compile(r"\$<\d+>")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]")
BLACK_FOREGROUND_RE = re.compile(r"color\s*:\s*(?:#000(?:000)?|black)\s*;?", re.IGNORECASE)
ANSI_CLEAR_RE = re.compile(r"\x1b\[[0-9;?]*[HfJ]")
WATCH_FRAME_SPLIT_RE = re.compile(r"\x1b\[[0-9;?]*2J\x1b\[[0-9;?]*H|\x1b\[[0-9;?]*H\x1b\[[0-9;?]*J")
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
        conn.execute(
            """
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
                fetched_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(server_readings)").fetchall()}
        if "connection_status" not in existing_columns:
            conn.execute("ALTER TABLE server_readings ADD COLUMN connection_status TEXT NOT NULL DEFAULT 'unknown'")
        if "stdout_html" not in existing_columns:
            conn.execute("ALTER TABLE server_readings ADD COLUMN stdout_html TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_server_readings_fetched_at
            ON server_readings (fetched_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_server_readings_ip_id
            ON server_readings (ip, id DESC)
            """
        )

        conn.execute(
            """
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
                fetched_at TEXT NOT NULL
            )
            """
        )
        existing_json_columns = {row[1] for row in conn.execute("PRAGMA table_info(server_json_readings)").fetchall()}
        if "connection_status" not in existing_json_columns:
            conn.execute("ALTER TABLE server_json_readings ADD COLUMN connection_status TEXT NOT NULL DEFAULT 'unknown'")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_server_json_readings_fetched_at
            ON server_json_readings (fetched_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_server_json_readings_ip_id
            ON server_json_readings (ip, id DESC)
            """
        )
        conn.commit()


def prune_old_readings(conn: sqlite3.Connection, fetched_at: str) -> None:
    cutoff = (datetime.fromisoformat(fetched_at) - timedelta(minutes=RETENTION_MINUTES)).isoformat()
    cursor = conn.execute(
        "DELETE FROM server_readings WHERE fetched_at < ?",
        (cutoff,),
    )
    if cursor.rowcount > 0:
        app.logger.info("已清理 server_readings 过期数据 %s 条", cursor.rowcount)


def prune_old_json_readings(conn: sqlite3.Connection, fetched_at: str) -> None:
    cutoff = (datetime.fromisoformat(fetched_at) - timedelta(minutes=RETENTION_MINUTES)).isoformat()
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
    cleaned = ANSI_CLEAR_RE.sub("", tail)
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
    stdout: str,
    stderr: str,
    exit_code: int | None,
    status: str,
    connection_status: str,
    fetched_at: str,
    json_stdout: str,
    json_exit_code: int | None,
    json_status: str,
) -> None:
    stdout_html = ansi_to_html_fragment(stdout)

    with db_lock:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO server_readings
                (server_name, ip, username, command, stdout, stdout_html, stderr, exit_code, status, connection_status, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server.get("name", "unknown"),
                    server.get("ip", ""),
                    server.get("username", ""),
                    COMMAND,
                    stdout,
                    stdout_html,
                    stderr,
                    exit_code,
                    status,
                    connection_status,
                    fetched_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO server_json_readings
                (server_name, ip, username, command, stdout_json, exit_code, status, connection_status, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    server.get("name", "unknown"),
                    server.get("ip", ""),
                    server.get("username", ""),
                    COMMAND_JSON,
                    json_stdout,
                    json_exit_code,
                    json_status,
                    connection_status,
                    fetched_at,
                ),
            )
            maybe_prune(conn, fetched_at)
            conn.commit()


def fetch_latest_readings() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT sr.server_name, sr.ip, sr.username, sr.command, sr.stdout, sr.stderr,
                     sr.stdout_html, sr.exit_code, sr.status, sr.connection_status, sr.fetched_at
            FROM server_readings sr
            JOIN (
                SELECT ip, MAX(id) AS max_id
                FROM server_readings
                GROUP BY ip
            ) latest ON sr.id = latest.max_id
            ORDER BY sr.server_name
            """
        ).fetchall()

    return [dict(row) for row in rows]


def fetch_latest_json_readings() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT sr.server_name, sr.ip, sr.username, sr.command, sr.stdout_json,
                   sr.exit_code, sr.status, sr.connection_status, sr.fetched_at
            FROM server_json_readings sr
            JOIN (
                SELECT ip, MAX(id) AS max_id
                FROM server_json_readings
                GROUP BY ip
            ) latest ON sr.id = latest.max_id
            ORDER BY sr.server_name
            """
        ).fetchall()

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


def load_server_name_to_ip_map() -> dict[str, str]:
    servers = load_servers(SERVERS_FILE)
    mapping: dict[str, str] = {}
    for server in servers:
        name = str(server.get("name", "")).strip()
        ip = str(server.get("ip", "")).strip()
        if name and ip:
            mapping[name] = ip
    return mapping


def start_watch_streams(client: paramiko.SSHClient):
    _, text_stdout, text_stderr = client.exec_command(COMMAND, get_pty=True)
    return text_stdout, text_stderr



def poll_server_forever(server: dict) -> None:
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

    while not stop_event.is_set():
        try:
            transport = client.get_transport() if client is not None else None
            if client is None or transport is None or not transport.is_active():
                client = connect_ssh_client(server)

                text_stdout, text_stderr = start_watch_streams(client)
                text_stream = ""
                text_stderr_stream = ""
                latest_json_text = ""
                latest_json_exit_code = None
                latest_json_status = "error"
                last_saved_at = 0.0
                last_json_polled_at = 0.0

            has_update = False

            if text_stdout is not None and text_stdout.channel.recv_ready():
                chunk = text_stdout.channel.recv(65535).decode("utf-8", errors="ignore")
                if chunk:
                    text_stream += chunk
                    has_update = True

            if text_stderr is not None and text_stderr.channel.recv_stderr_ready():
                err_chunk = text_stderr.channel.recv_stderr(65535).decode("utf-8", errors="ignore")
                if err_chunk:
                    text_stderr_stream = (text_stderr_stream + err_chunk)[-20000:]

            now = time.monotonic()
            if now - last_json_polled_at >= POLL_INTERVAL_SECONDS:
                try:
                    _, json_stdout, json_stderr = client.exec_command(COMMAND_JSON, get_pty=False)
                    latest_json_exit_code = json_stdout.channel.recv_exit_status()
                    latest_json_text = json_stdout.read().decode("utf-8", errors="ignore").strip()
                    json_err = json_stderr.read().decode("utf-8", errors="ignore").strip()
                    if json_err:
                        text_stderr_stream = (text_stderr_stream + "\n" + json_err)[-20000:]
                    latest_json_status = "ok" if latest_json_exit_code == 0 and latest_json_text else "error"
                except Exception:
                    latest_json_text = ""
                    latest_json_exit_code = None
                    latest_json_status = "error"
                last_json_polled_at = now
                has_update = True

            if has_update and now - last_saved_at >= save_interval:
                fetched_at = utc_now_iso()
                transport = client.get_transport()
                connection_status = "connected" if transport is not None and transport.is_active() else "disconnected"

                normalized_text = normalize_stream_text(text_stream)
                status = "ok" if normalized_text else "error"

                save_poll_result(
                    server=server,
                    stdout=normalized_text,
                    stderr=text_stderr_stream,
                    exit_code=0 if status == "ok" else None,
                    status=status,
                    connection_status=connection_status,
                    fetched_at=fetched_at,
                    json_stdout=latest_json_text,
                    json_exit_code=latest_json_exit_code,
                    json_status=latest_json_status,
                )
                last_saved_at = now

            if stop_event.wait(0.2):
                break

        except Exception as exc:
            fetched_at = utc_now_iso()
            save_poll_result(
                server=server,
                stdout="",
                stderr="",
                exit_code=None,
                status="error",
                connection_status="disconnected",
                fetched_at=fetched_at,
                json_stdout="",
                json_exit_code=None,
                json_status="error",
            )
            if client is not None:
                client.close()
                client = None
            text_stdout = None
            text_stderr = None
            if stop_event.wait(POLL_INTERVAL_SECONDS):
                break

    if client is not None:
        client.close()


def start_polling_threads() -> None:
    global polling_started
    if polling_started:
        return

    servers = load_servers(SERVERS_FILE)

    for server in servers:
        thread = threading.Thread(target=poll_server_forever, args=(server,), daemon=True)
        thread.start()

    polling_started = True


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
    return jsonify({
        "interval_seconds": POLL_INTERVAL_SECONDS,
        "data": fetch_latest_readings(),
    })


@app.route("/health", methods=["GET"])
def health() -> Response:
    return jsonify({
        "status": "ok",
        "polling_started": polling_started,
        "is_collector_process": is_collector_process,
        "interval_seconds": POLL_INTERVAL_SECONDS,
        "db_exists": DB_FILE.exists(),
    })


@app.route("/jinfo", methods=["GET"])
def jinfo() -> Response:
    return jsonify({
        "interval_seconds": POLL_INTERVAL_SECONDS,
        "data": fetch_latest_json_readings(),
    })


@app.route("/set_gpu", methods=["GET"])
def set_gpu() -> Response:
    try:
        content = (BASE_DIR / "assets" / "set_gpu.py").read_text(encoding="utf-8")
    except Exception:
        return Response("assets/set_gpu.py not found", status=404, mimetype="text/plain")

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
            exclude_ips = [server_name_to_ip[name] for name in ex_values if name in server_name_to_ip]
            content = re.sub(
                r"(^\s*EXCLUDE_SERVERS_F5rQa\s*=\s*)(.+?)(\s*#\s*sym:EXCLUDE_SERVERS\s*$)",
                rf"\g<1>{exclude_ips!r}\g<3>",
                content,
                flags=re.MULTILINE,
            )
    except ValueError:
        return Response("t1 and t2 must be floating point numbers", status=400, mimetype="text/plain")

    return Response(content, mimetype="text/plain")


@app.route("/auto_set_gpu_example", methods=["GET"])
def auto_set_gpu_example() -> Response:
    try:
        content = (BASE_DIR / "assets" / "auto_set_gpu_example.py").read_text(encoding="utf-8")
    except Exception:
        return Response("assets/auto_set_gpu_example.py not found", status=404, mimetype="text/plain")

    return Response(content, mimetype="text/plain")


@app.route("/auto_set_gpu_oneline", methods=["GET"])
def auto_set_gpu_oneline() -> Response:
    try:
        content = (BASE_DIR / "assets" / "auto_set_gpu_oneline.py").read_text(encoding="utf-8")
    except Exception:
        return Response("assets/auto_set_gpu_oneline.py not found", status=404, mimetype="text/plain")

    return Response(content, mimetype="text/plain")


@app.route("/", methods=["GET"])
def index() -> Response:
    return Response((BASE_DIR / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


def main() -> None:
    ensure_runtime_initialized()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "2223")), debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
