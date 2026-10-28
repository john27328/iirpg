"""Small dependency-free HTTP server for the chat prototype."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from .application import ChatService
from .dice import DEFAULT_RULES, DiceTool
from .storage import ChatStore, MessageConflict

WEB_ROOT = Path(__file__).with_name("web")
ROOM_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/messages\Z")
DICE_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/tools/dice\Z")
MAX_BODY_SIZE = 16384
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], database: Path, access_token: str = "",
                 dice: DiceTool | None = None) -> None:
        self.service = ChatService(ChatStore(database), dice)
        self.access_token = access_token
        super().__init__(address, ChatHandler)


class ChatHandler(BaseHTTPRequestHandler):
    server: ChatHTTPServer
    server_version = "IIRPG/0.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._respond(status, json.dumps(payload, ensure_ascii=False).encode(),
                      "application/json; charset=utf-8")

    def _body(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Ожидается application/json.")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Неподдерживаемый формат запроса.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Некорректная длина запроса.") from None
        if not 0 < length <= MAX_BODY_SIZE:
            raise ValueError("Пустой или слишком большой запрос.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ValueError("Некорректный JSON.") from None
        if not isinstance(payload, dict):
            raise ValueError("Ожидается объект JSON.")
        return payload

    @staticmethod
    def _message_response(message: dict) -> dict:
        response = {"message": message}
        if message["type"] == "tool.result":
            response.update({"result": message["tool_result"]["result"],
                             "success": message["tool_result"]["success"]})
        return response

    def _dispatch(self, method: str) -> None:
        route = urlsplit(self.path)
        if method == "GET" and route.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[route.path]
            self._respond(200, (WEB_ROOT / filename).read_bytes(), content_type)
            return
        if method == "GET" and route.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if route.path.startswith("/api/") and self.server.access_token:
            received = self.headers.get("Authorization", "").encode("utf-8")
            expected = f"Bearer {self.server.access_token}".encode("utf-8")
            if not secrets.compare_digest(received, expected):
                self._json(401, {"error": "Нужен ключ доступа из вашей секретной ссылки."})
                return
        if method == "POST" and route.path == "/api/rooms":
            self._body()
            self._json(201, {"room_id": self.server.service.store.create_room()})
            return
        if method == "GET" and route.path == "/api/tools/dice":
            self._json(200, self.server.service.dice.describe())
            return
        dice_match = DICE_ROUTE.fullmatch(route.path)
        if method == "POST" and dice_match:
            payload = {**self._body(), "type": "tool.call", "tool": "dice.roll"}
            message = self.server.service.send(dice_match.group(1), payload)
            self._json(201, self._message_response(message))
            return
        match = ROOM_ROUTE.fullmatch(route.path)
        if match:
            room_id = match.group(1)
            if method == "GET":
                query = parse_qs(route.query)
                after = int(query.get("after", ["0"])[0])
                if not 0 <= after <= 9223372036854775807:
                    raise ValueError("Некорректный указатель истории.")
                page = self.server.service.messages(room_id, query.get("chat", [""])[0], after)
                self._json(200, page)
                return
            if method == "POST":
                self._json(201, self._message_response(self.server.service.send(room_id, self._body())))
                return
        self._json(404, {"error": "Страница не найдена."})

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except MessageConflict as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Комната не найдена."})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": "Ошибка сервера. Попробуйте ещё раз."})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def main() -> None:
    parser = argparse.ArgumentParser(description="A chat and JSON test stub on a Python server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", type=Path, default=Path(".local/chat.sqlite3"))
    parser.add_argument("--dice-rules", type=Path, default=DEFAULT_RULES,
                        help="JSON file defining dice system formats")
    parser.add_argument("--access-token-file", type=Path,
                        help="Require a bearer token from this file for all chat API requests")
    arguments = parser.parse_args()
    token = ""
    if arguments.access_token_file:
        token = arguments.access_token_file.read_text().strip()
        if len(token) < 32:
            parser.error("The access token must contain at least 32 characters.")
    with ChatHTTPServer((arguments.host, arguments.port), arguments.database, token,
                        DiceTool(arguments.dice_rules)) as server:
        print(f"IIRPG Chat: http://{arguments.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.", flush=True)


if __name__ == "__main__":
    main()
