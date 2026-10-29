"""Small dependency-free HTTP server for the chat prototype."""

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from .ai import AIService, DEFAULT_AI_CONFIG, OpenRouterClient, load_config
from .scene import SceneStore
from .combat import CombatStore
from .application import ChatService
from .character_sheet import DEFAULT_CHARACTER_RULES, CharacterRules
from .commands import describe_commands
from .dice import DEFAULT_RULES, DiceTool
from .debug import inspect_room
from .roster import DEFAULT_INVENTORY_RULES, InventoryRules
from .roster_storage import RosterStore
from .storage import ChatStore, MessageConflict

WEB_ROOT = Path(__file__).with_name("web")
ROOM_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/messages\Z")
DICE_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/tools/dice\Z")
COMMAND_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/gm/commands\Z")
ROSTER_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/roster\Z")
DEBUG_ROUTE = re.compile(r"/api/rooms/([0-9a-f-]{36})/debug\Z")
MAX_BODY_SIZE = 65536
STATIC_FILES = {
    "/battle": ("battle.html", "text/html; charset=utf-8"),
    "/battle/": ("battle.html", "text/html; charset=utf-8"),
    "/battle.js": ("battle.js", "text/javascript; charset=utf-8"),
    "/battle.css": ("battle.css", "text/css; charset=utf-8"),
    "/room-session.js": ("room-session.js", "text/javascript; charset=utf-8"),
    "/scene.js": ("scene.js", "text/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/debug": ("debug.html", "text/html; charset=utf-8"),
    "/debug/": ("debug.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/inventory.js": ("inventory.js", "text/javascript; charset=utf-8"),
    "/debug.js": ("debug.js", "text/javascript; charset=utf-8"),
    "/character-sheet.js": ("character-sheet.js", "text/javascript; charset=utf-8"),
}
RULE_FILES = {
    "/inventory-ORC-NOTICE.md": ("inventory-ORC-NOTICE.md", "text/plain; charset=utf-8"),
    "/inventory-LICENSE-ORC.pdf": ("LICENSE-ORC.pdf", "application/pdf"),
    "/character-ORC-NOTICE.md": ("character-ORC-NOTICE.md", "text/plain; charset=utf-8"),
}


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], database: Path, access_token: str = "",
                 dice: DiceTool | None = None, inventory_rules: InventoryRules | None = None,
                 character_rules: CharacterRules | None = None,
                 ai_config: Path = DEFAULT_AI_CONFIG,
                 openrouter_key_file: Path = Path('.local/openrouter-key')) -> None:
        self.service = ChatService(ChatStore(database), dice)
        self.roster = RosterStore(self.service.store, inventory_rules or InventoryRules.load(), character_rules)
        self.combat = CombatStore(self.roster, self.service.dice.roll_die)
        self.scene = SceneStore(self.roster, self.service.dice.roll_die)
        self.ai = AIService(self.combat, self.scene, load_config(ai_config), OpenRouterClient(openrouter_key_file))
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

    @staticmethod
    def _cursor(query_string: str) -> int:
        after = int(parse_qs(query_string).get("after", ["0"])[0])
        if not 0 <= after <= 9223372036854775807:
            raise ValueError("Некорректный указатель истории.")
        return after

    def _dispatch(self, method: str) -> None:
        route = urlsplit(self.path)
        if method == "GET" and route.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[route.path]
            self._respond(200, (WEB_ROOT / filename).read_bytes(), content_type)
            return
        if method == "GET" and route.path in RULE_FILES:
            filename, content_type = RULE_FILES[route.path]
            self._respond(200, (DEFAULT_INVENTORY_RULES.parent / filename).read_bytes(), content_type)
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
        if method == "GET" and route.path == "/api/gm/commands":
            self._json(200, describe_commands())
            return
        if method == 'GET' and route.path == '/api/ai':
            self._json(200, self.server.ai.status())
            return
        if method == 'GET' and route.path == '/api/combat/groups':
            self._json(200, {'groups': self.server.combat.rules['groups']})
            return
        if method == 'GET' and route.path == '/api/combat/presets':
            self._json(200, {'presets': self.server.combat.presets})
            return
        ai_match = re.fullmatch(r'/api/rooms/([0-9a-f-]{36})/ai/npc/(context|step)', route.path)
        if ai_match:
            if method == 'GET' and ai_match.group(2) == 'context':
                self._json(200, self.server.ai.preview(ai_match.group(1)))
                return
            if method == 'POST' and ai_match.group(2) == 'step':
                self._json(200, self.server.ai.step(ai_match.group(1), self._body(), self.headers.get('Idempotency-Key', '')))
                return
        if method == "GET" and route.path == "/api/maps/schema":
            self._json(200, json.loads((DEFAULT_INVENTORY_RULES.parent / "map_schema.json").read_text()))
            return
        if method == "GET" and route.path == "/api/maps":
            self._json(200, {"templates": self.server.scene.maps, "format": "iirpg-map-v1"})
            return
        scene_match = re.fullmatch(r"/api/rooms/([0-9a-f-]{36})/scene", route.path)
        if scene_match:
            if method == "GET":
                self._json(200, self.server.scene.read(scene_match.group(1)))
                return
            if method == "POST":
                self._json(200, self.server.scene.execute(scene_match.group(1), self._body(), self.headers.get("Idempotency-Key", "")))
                return
        combat_match = re.fullmatch(r"/api/rooms/([0-9a-f-]{36})/combat", route.path)
        if combat_match:
            if method == "GET":
                self._json(200, self.server.combat.read(combat_match.group(1)))
                return
            if method == "POST":
                self._json(200, self.server.combat.execute(combat_match.group(1), self._body(), self.headers.get("Idempotency-Key", "")))
                return
        debug_match = DEBUG_ROUTE.fullmatch(route.path)
        if method == "GET" and debug_match:
            self._json(200, inspect_room(self.server.service, self.server.roster,
                                        debug_match.group(1), self._cursor(route.query), self.server.combat, self.server.scene, self.server.ai))
            return
        roster_match = ROSTER_ROUTE.fullmatch(route.path)
        if roster_match:
            if method == "GET":
                self._json(200, self.server.roster.read(roster_match.group(1)))
                return
            if method == "POST":
                self._json(200, self.server.roster.execute(
                    roster_match.group(1), self._body(), self.headers.get("Idempotency-Key", "")))
                return
        command_match = COMMAND_ROUTE.fullmatch(route.path)
        if method == "POST" and command_match:
            request_id = self.headers.get("Idempotency-Key")
            if not request_id:
                raise ValueError("Нужен служебный заголовок Idempotency-Key; клиент добавляет его автоматически.")
            message = self.server.service.send_command(command_match.group(1), self._body(), request_id)
            self._json(201, self._message_response(message))
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
                after = self._cursor(route.query)
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
        except KeyError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error.args[0])})
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
    parser.add_argument("--inventory-rules", type=Path, default=DEFAULT_INVENTORY_RULES,
                        help="Inventory configuration saved for each room on first roster access")
    parser.add_argument("--character-rules", type=Path, default=DEFAULT_CHARACTER_RULES,
                        help="Character sheet fields, profiles and formulas saved per room")
    parser.add_argument("--access-token-file", type=Path,
                        help="Require a bearer token from this file for all chat API requests")
    parser.add_argument('--ai-config', type=Path, default=DEFAULT_AI_CONFIG,
                        help='NPC profile, free OpenRouter model and request limits')
    parser.add_argument('--openrouter-key-file', type=Path, default=Path('.local/openrouter-key'),
                        help='Server-only OpenRouter key; read at each request (or OPENROUTER_API_KEY)')
    arguments = parser.parse_args()
    token = ""
    if arguments.access_token_file:
        token = arguments.access_token_file.read_text().strip()
        if len(token) < 32:
            parser.error("The access token must contain at least 32 characters.")
    with ChatHTTPServer((arguments.host, arguments.port), arguments.database, token,
                        DiceTool(arguments.dice_rules), InventoryRules.load(arguments.inventory_rules),
                        CharacterRules.load(arguments.character_rules), arguments.ai_config,
                        arguments.openrouter_key_file) as server:
        print(f"IIRPG Chat: http://{arguments.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.", flush=True)


if __name__ == "__main__":
    main()
