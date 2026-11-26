"""Request/response over a host's named pipe (stdlib multiprocessing.connection, HMAC auth).

Requests and responses are single JSON messages. An `attach` request turns the
connection into a stream of frames: b"O"+output from the host; b"I"+input,
b"R"+json(rows, cols) and b"D" (detach) from the client; b"X"+json when the agent exits.
"""

from __future__ import annotations

import json
from multiprocessing.connection import Client, Connection


def connect(info: dict) -> Connection:
    return Client(info["pipe"], family="AF_PIPE", authkey=bytes.fromhex(info["authkey"]))


def request(info: dict, req: dict, timeout: float = 5.0) -> dict:
    conn = connect(info)
    try:
        conn.send_bytes(json.dumps(req).encode("utf-8"))
        if not conn.poll(timeout):
            raise TimeoutError(f"host did not answer {req.get('op')!r} within {timeout}s")
        return json.loads(conn.recv_bytes().decode("utf-8"))
    finally:
        conn.close()
