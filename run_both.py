"""Single-command orchestrator for the ScopeEnvelope web viewer
(visualization-plan.md §5): starts webviz.server:app in-process, waits until
it's accepting connections AND its Bridge/Change-Stream watchers are up
(via /health), opens a browser tab, kicks off the real agent runs (lazy
then envelope) off the main thread so the server loop keeps serving SSE
while the agent makes LLM calls, then stays alive after both finish
(Ctrl+C to exit) -- the finished page persists.

    python run_both.py "PIPE-2031: Implement Kafka consumer"
    python run_both.py   # uses DEFAULT_TICKET below
"""

import asyncio
import http.client
import json
import os
import socket
import sys
import threading
import webbrowser

import uvicorn

from agent import run_ticket

HOST = "127.0.0.1"
PORT = 8765
DEFAULT_TICKET = "PIPE-2031: Implement Kafka consumer"


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((host, port)) == 0


async def _wait_for_server(host: str, port: int) -> None:
    while not _port_open(host, port):
        await asyncio.sleep(0.05)


def _bridge_ready(host: str, port: int) -> bool:
    conn = http.client.HTTPConnection(host, port, timeout=0.5)
    try:
        conn.request("GET", "/health")
        data = json.loads(conn.getresponse().read())
        return bool(data.get("ready"))
    except OSError:
        return False
    finally:
        conn.close()


async def _wait_for_bridge(host: str, port: int) -> None:
    while not _bridge_ready(host, port):
        await asyncio.sleep(0.05)


def _drive_runs(ticket: str) -> None:
    run_ticket(ticket, "lazy")
    run_ticket(ticket, "envelope")


async def main() -> None:
    ticket = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TICKET
    os.environ["WEBVIZ_TICKET"] = ticket

    config = uvicorn.Config("webviz.server:app", host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve())
    await _wait_for_server(HOST, PORT)
    await _wait_for_bridge(HOST, PORT)
    webbrowser.open(f"http://{HOST}:{PORT}/")

    threading.Thread(target=_drive_runs, args=(ticket,), daemon=True).start()

    await server_task


if __name__ == "__main__":
    asyncio.run(main())
