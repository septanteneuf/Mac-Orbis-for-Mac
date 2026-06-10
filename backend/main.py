import asyncio
import json
import socket
import sqlite3
import subprocess
import maxminddb
import re

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from pathlib import Path
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "mac_orbis.sqlite3"
NETTOP_PROCESS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d+,(.+)\.(\d+),,,")
NETTOP_CONNECTION_RE = re.compile(

    r"^\d{2}:\d{2}:\d{2}\.\d+,(tcp[46]) ([^<]+)<->([^,]+),([^,]*),([^,]*),"

)

app = FastAPI(title="Mac Orbis")

app.mount(
    "/assets",
    StaticFiles(directory=BASE_DIR.parent / "frontend" / "static"),
    name="assets"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

geo_db = maxminddb.open_database(

    "backend/data/GeoLite2-City.mmdb"

)

def load_header():
    return Path(
        "frontend/templates/header.html"
    ).read_text(encoding="utf-8")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                process TEXT NOT NULL,
                pid INTEGER NOT NULL,
                user TEXT,
                local_ip TEXT,
                local_port TEXT,
                remote_ip TEXT,
                remote_port TEXT,
                remote_hostname TEXT,
                service TEXT,
                is_local INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recent_unique
            ON connections (
                timestamp, process, pid, remote_ip, remote_port
            )
        """)


def split_host_port(value: str):
    value = value.strip()

    if value.startswith("[") and "]:" in value:
        host, port = value.rsplit("]:", 1)
        return host[1:], port

    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host, port

    return value, None


def is_local_ip(ip: str):
    if not ip:
        return True

    return (
        ip.startswith("127.")
        or ip == "::1"
        or ip.startswith("192.168.")
        or ip.startswith("10.")
        or ip.startswith("172.16.")
        or ip.startswith("172.17.")
        or ip.startswith("172.18.")
        or ip.startswith("172.19.")
        or ip.startswith("172.2")
        or ip.startswith("172.30.")
        or ip.startswith("172.31.")
        or ip.startswith("fe80:")
    )


@lru_cache(maxsize=4096)
def reverse_dns(ip: str):
    if not ip or is_local_ip(ip):
        return None

    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname
    except Exception:
        return None


def detect_service(hostname, ip):
    value = (hostname or ip or "").lower()

    rules = {
        "1e100.net": "Google",
        "google": "Google",
        "gmail": "Google",
        "infomaniak.com": "Infomaniak",
        "icloud.com": "Apple",
        "apple.com": "Apple",
        "17.": "Apple",
        "box.com": "Box",
        "free.fr": "Free",
        "cloudflare": "Cloudflare",
        "akamai": "Akamai",
    }

    for key, name in rules.items():
        if key in value or value.startswith(key):
            return name

    if is_local_ip(value):
        return "Local"

    return "Inconnu"


def collect_lsof_connections():
    result = subprocess.run(
        ["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"],
        capture_output=True,
        text=True
    )

    connections = {}

    for line in result.stdout.splitlines()[1:]:
        parts = line.split()

        if len(parts) < 9:
            continue

        process = parts[0]
        pid = parts[1]
        user = parts[2]

        address = None

        for part in parts:
            if "->" in part:
                address = part
                break

        if not address:
            continue

        local, remote = address.split("->", 1)

        local_ip, local_port = split_host_port(local)
        remote_ip, remote_port = split_host_port(remote)

        remote_hostname = None
        local_status = is_local_ip(remote_ip)
        service = detect_service(remote_hostname, remote_ip)
        geo = get_geo(remote_ip)

        key = f"{process}:{pid}:{remote_hostname or remote_ip}:{remote_port}"

        connections[key] = {
            "process": process,
            "pid": int(pid),
            "user": user,
            "local_ip": local_ip,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "remote_hostname": remote_hostname,
            "service": service,
            "country": geo["country"],
            "country_code": geo["country_code"],
            "city": geo["city"],
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "is_local": local_status,
        }

    return list(connections.values())

def split_nettop_endpoint(value):
    value = value.strip()

    if value in ("*:*", "*.*", "*"):
        return None, None

    if ":" in value and value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        return host, port

    parts = value.split(".")
    if len(parts) == 5 and all(p.isdigit() for p in parts):
        host = ".".join(parts[:4])
        port = parts[4]
        return host, port

    if ":" in value and "." in value:
        host, port = value.rsplit(".", 1)
        return host, port

    return value, None


def collect_nettop_connections():
    process = subprocess.Popen(
        ["nettop", "-L", "1", "-m", "tcp", "-x"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        
        print("NETTOP stdout lignes:", len(stdout.splitlines()))
        print("NETTOP stderr:", stderr[:300])

    current_process = None
    current_pid = None
    connections = {}

    for line in stdout.splitlines():
        process_match = NETTOP_PROCESS_RE.match(line)

        if process_match:
            current_process = process_match.group(1)
            current_pid = int(process_match.group(2))
            continue

        connection_match = NETTOP_CONNECTION_RE.match(line)

        if not connection_match or not current_process:
            continue

        protocol = connection_match.group(1)
        local_raw = connection_match.group(2)
        remote_raw = connection_match.group(3)
        interface = connection_match.group(4)
        state = connection_match.group(5)

        if state != "Established":
            continue

        local_ip, local_port = split_nettop_endpoint(local_raw)
        remote_ip, remote_port = split_nettop_endpoint(remote_raw)

        if not remote_ip:
            continue

        remote_hostname = None
        local_status = is_local_ip(remote_ip)
        service = detect_service(remote_hostname, remote_ip)
        geo = get_geo(remote_ip)

        key = f"{current_process}:{current_pid}:{remote_ip}:{remote_port}"

        connections[key] = {
            "process": current_process,
            "pid": current_pid,
            "user": "",
            "local_ip": local_ip,
            "local_port": local_port,
            "remote_ip": remote_ip,
            "remote_port": remote_port,
            "remote_hostname": remote_hostname,
            "service": service,
            "country": geo["country"],
            "country_code": geo["country_code"],
            "city": geo["city"],
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "is_local": local_status,
            "protocol": protocol,
            "interface": interface,
            "state": state,
            "source": "nettop",
        }

    return list(connections.values())

def save_history(connections):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with sqlite3.connect(DB_PATH) as conn:
        for c in connections:
            conn.execute("""
                INSERT OR IGNORE INTO connections (
                    timestamp,
                    process,
                    pid,
                    user,
                    local_ip,
                    local_port,
                    remote_ip,
                    remote_port,
                    remote_hostname,
                    service,
                    is_local
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                c["process"],
                c["pid"],
                c["user"],
                c["local_ip"],
                c["local_port"],
                c["remote_ip"],
                c["remote_port"],
                c["remote_hostname"],
                c["service"],
                1 if c["is_local"] else 0,
            ))


def get_connections():
    connections = collect_lsof_connections()
    save_history(connections)
    return connections

#def get_connections():
#    connections = collect_nettop_connections()
#    save_history(connections)
#    return connections

@app.get("/header", response_class=HTMLResponse)
def header():
    return (BASE_DIR.parent / "frontend" / "template" / "header.html").read_text(encoding="utf-8")

@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/graph")
def graph():
    return FileResponse(BASE_DIR / "static" / "graph.html")


@app.get("/connections")
def connections():
    return get_connections()


@app.get("/history")
def history(limit: int = 200):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT *
            FROM connections
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return [dict(row) for row in rows]

@app.get("/history-view")
def history_view():
    return FileResponse("backend/static/history.html")

@app.get("/stats")
def stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        services = conn.execute("""
            SELECT service, COUNT(*) as count
            FROM connections
            GROUP BY service
            ORDER BY count DESC
        """).fetchall()

        processes = conn.execute("""
            SELECT process, COUNT(*) as count
            FROM connections
            GROUP BY process
            ORDER BY count DESC
        """).fetchall()

    return {
        "services": [dict(row) for row in services],
        "processes": [dict(row) for row in processes],
    }

@app.get("/world")
def world():
    return FileResponse(BASE_DIR / "static" / "world.html")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    while True:
        await websocket.send_text(json.dumps(get_connections()))
        await asyncio.sleep(2)
        
def get_geo(ip):
    try:
        result = geo_db.get(ip)

        if not result:
            return {
                "country": None,
                "country_code": None,
                "city": None,
                "latitude": None,
                "longitude": None,
            }

        country_data = result.get("country", {})
        city_data = result.get("city", {})
        location_data = result.get("location", {})

        return {
            "country": country_data.get("names", {}).get("fr") or country_data.get("names", {}).get("en"),
            "country_code": country_data.get("iso_code"),
            "city": city_data.get("names", {}).get("fr") or city_data.get("names", {}).get("en"),
            "latitude": location_data.get("latitude"),
            "longitude": location_data.get("longitude"),
        }

    except Exception:
        return {
            "country": None,
            "country_code": None,
            "city": None,
            "latitude": None,
            "longitude": None,
        }