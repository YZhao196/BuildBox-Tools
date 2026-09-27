"""The protocol over HTTP, using nothing but the standard library.

This exists so the library can be used on its own — a script, a workshop machine,
a research rig — without anyone having to pick a web framework. It is a thin
adapter and deliberately holds no logic: `Management.handle` decides everything,
and this turns a request into its arguments and its answer into a response.

An embedding program that already runs a server should call `handle` from its own
routes instead; nothing here is required for the library to work.

Threads, not a single loop, because the long poll is a request that is *meant* to
be held open — on one thread it would stop every other device from being heard.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from .management import Management, Response

log = logging.getLogger("buildbox_management")

#: How long a connection may sit idle between requests.
#:
#: Comfortably longer than a long poll, since a poll is a request in flight and
#: not an idle connection — timing it out mid-poll would look to the device like
#: the network dropping, once per poll.
IDLE_TIMEOUT_SECONDS = 120.0

#: The largest request body that will be read.
#:
#: Generous, because a camera frame arrives as a base64 data URL and is much
#: larger than anything else — but not unlimited, because a device is a machine
#: on the other end of the network and its Content-Length is not a promise this
#: process should believe. A body past this is refused unread.
MAX_BODY_BYTES = 16 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    #: Keep-alive, because a device polls repeatedly and reconnecting every time
    #: is the difference between a heartbeat and a TCP handshake per beat.
    protocol_version = "HTTP/1.1"
    server_version = "buildbox_management"

    def do_GET(self) -> None:  # noqa: N802 - the name BaseHTTPRequestHandler calls
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0

        if length > MAX_BODY_BYTES:
            # Refused before it is read. A big Content-Length is not evidence
            # the bytes are coming, and reading it anyway is how one device
            # takes the whole process down. The connection is closed because
            # whatever the peer does send would otherwise be parsed as the next
            # request on a keep-alive socket.
            self.close_connection = True
            self._respond(
                Response(
                    413,
                    {
                        "error": "too-large",
                        "message": f"A body may be at most {MAX_BODY_BYTES} bytes.",
                    },
                )
            )
            return

        body = self.rfile.read(length) if length > 0 else None

        parts = urlsplit(self.path)
        headers: Dict[str, str] = {name: value for name, value in self.headers.items()}

        try:
            # The one Management the server serves, reached through the server
            # rather than set on each handler — a handler is made per request.
            response = self.server.management.handle(
                method, parts.path, query=parts.query, headers=headers, body=body
            )
        except Exception:  # noqa: BLE001 - a handler threw; the device must learn
            # An unhandled failure means the reading was not taken delivery of.
            # Answering success would let the device drop it on the floor.
            log.exception("Unhandled error handling %s %s", method, parts.path)
            response = Response(
                500,
                {"error": "internal", "message": "The receiver failed to handle that."},
            )

        self._respond(response)

    def _respond(self, response: Response) -> None:
        if response.body is None:
            payload = b""
        else:
            payload = json.dumps(response.body).encode("utf-8")

        # A long poll answers 204 with no body at all, which is how the device
        # tells "nothing to do" from "the network is broken".
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if payload:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload and response.status != 204:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default writes to stderr, which in a library is noise the embedding
        # program did not ask for. It goes to this module's logger instead, where
        # it can be configured or dropped.
        log.debug("%s - %s", self.address_string(), fmt % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    management: Management

    def __init__(self, address: Tuple[str, int], management: Management) -> None:
        self.management = management
        super().__init__(address, _Handler)

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(IDLE_TIMEOUT_SECONDS)
        super().finish_request(request, client_address)


def serve(
    management: Management,
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    block: bool = True,
) -> _Server:
    """Serve `management` over HTTP. Returns the server, started unless `block`.

    With ``block=False`` the server runs on a daemon thread and the caller can
    move on — which is what a program with a loop of its own wants, and what the
    tests use to exercise a real device against a real socket.
    """
    server = _Server((host, port), management)
    if block:
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server
