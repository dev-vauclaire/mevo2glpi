from __future__ import annotations

import json
import socket
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from urllib.error import URLError
from urllib.request import urlopen

from glpi_ticketing_web.application import build_server
from glpi_ticketing_web.settings import WebSettings

STARTUP_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0


def test_composed_uvicorn_server_is_reachable_on_loopback(tmp_path: Path) -> None:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()

    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listening_socket.bind(("127.0.0.1", 0))
    listening_socket.listen(2048)
    random_port = listening_socket.getsockname()[1]

    settings = WebSettings(
        audio_root=audio_root,
        database_url=("postgresql+psycopg://web:synthetic@127.0.0.1:5432/jobs"),
        web_host="127.0.0.1",
        web_port=random_port,
        _env_file=None,
    )
    server = build_server(settings)
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run(sockets=[listening_socket])
        except BaseException as error:  # pragma: no cover - asserted by main thread
            server_errors.append(error)

    server_thread = Thread(target=serve, name="test-web-server", daemon=True)
    server_thread.start()

    poll_wait = Event()
    deadline = monotonic() + STARTUP_TIMEOUT_SECONDS
    response_status: int | None = None
    response_body: dict[str, str] | None = None
    try:
        while monotonic() < deadline:
            try:
                with urlopen(  # noqa: S310 - fixed loopback test URL
                    f"http://127.0.0.1:{random_port}/health",
                    timeout=0.25,
                ) as response:
                    response_status = response.status
                    response_body = json.load(response)
                break
            except (ConnectionError, TimeoutError, URLError):
                poll_wait.wait(0.02)
    finally:
        server.should_exit = True
        server_thread.join(SHUTDOWN_TIMEOUT_SECONDS)
        listening_socket.close()

    assert not server_errors
    assert response_status == 200
    assert response_body == {"status": "ok"}
    assert not server_thread.is_alive()
