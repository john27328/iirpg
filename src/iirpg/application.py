"""Publishing to a shared queue and reading independent subscriber views."""

from uuid import UUID

from .domain import Chat, Visibility
from .storage import ChatStore


class ChatService:
    def __init__(self, store: ChatStore) -> None:
        self.store = store
        first = Chat("first", "Чат 01")
        second = Chat("second", "Чат 02")
        self.chats = {chat.key: chat for chat in (first, second)}

    def _visibility(self, value: object) -> Visibility:
        if not isinstance(value, dict):
            raise ValueError("visibility должен быть объектом JSON.")
        mode = value.get("mode")
        ids = value.get("chat_ids", [])
        if mode not in ("all", "selected"):
            raise ValueError("Режим видимости: all или selected.")
        if not isinstance(ids, list) or any(not isinstance(key, str) or key not in self.chats for key in ids):
            raise ValueError("В видимости указаны неизвестные чаты.")
        if mode == "all" and ids:
            raise ValueError("В режиме all список chat_ids должен быть пустым.")
        if mode == "selected" and not ids:
            raise ValueError("Выберите хотя бы один чат.")
        return Visibility(mode, tuple(sorted(set(ids))))

    def messages(self, room_id: str, chat_id: str, after: int = 0) -> dict:
        if not isinstance(chat_id, str) or chat_id not in self.chats:
            raise ValueError("Укажите чат-подписчик: first или second.")
        page = self.store.page(room_id, after)
        chat = self.chats[chat_id]
        return {**page, "messages": [message for message in page["messages"] if chat.can_see(message)]}

    def send(self, room_id: str, payload: dict) -> dict:
        sender = payload.get("sender")
        if not isinstance(sender, str) or sender not in self.chats:
            raise ValueError("Неизвестный отправитель.")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            raise ValueError("Нужен идентификатор сообщения.")
        try:
            request_id = str(UUID(request_id))
        except ValueError:
            raise ValueError("Некорректный идентификатор сообщения.") from None
        if payload.get("type", "chat.message") != "chat.message":
            raise ValueError("Неизвестный тип события.")
        visibility = self._visibility(payload.get("visibility", {"mode": "all"}))
        if "metadata" in payload and not isinstance(payload["metadata"], dict):
            raise ValueError("metadata должен быть объектом JSON.")
        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("choices должен быть массивом строк.")
        message = self.chats[sender].send(payload.get("text"), visibility, payload.get("metadata"), tuple(choices))
        return self.store.append(room_id, request_id, message)
