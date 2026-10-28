"""Queue messages and subscriber visibility, independent of HTTP and storage."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json

MAX_MESSAGE_LENGTH = 2000
MAX_METADATA_BYTES = 4096
MAX_CHOICES = 10
MAX_CHOICE_LENGTH = 200


@dataclass(frozen=True, slots=True)
class Visibility:
    mode: str = "all"
    chat_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Message:
    sender: str
    text: str
    created_at: str
    visibility: Visibility
    metadata: dict
    choices: tuple[str, ...] = ()
    type: str = "chat.message"
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "sender": self.sender,
            "type": self.type,
            "text": self.text,
            "visibility": {"mode": self.visibility.mode, "chat_ids": list(self.visibility.chat_ids)},
            "metadata": self.metadata,
            "choices": list(self.choices),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Chat:
    key: str
    name: str

    def send(self, text: str, visibility: Visibility = Visibility(), metadata: dict | None = None,
             choices: tuple[str, ...] = ()) -> Message:
        """Prepare a queue entry without a direct connection to another chat."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Введите сообщение.")
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"Максимум {MAX_MESSAGE_LENGTH} символов в сообщении.")
        if not isinstance(choices, tuple) or len(choices) > MAX_CHOICES:
            raise ValueError(f"Допускается не более {MAX_CHOICES} вариантов ответа.")
        if any(not isinstance(choice, str) or not choice.strip() or len(choice) > MAX_CHOICE_LENGTH
               for choice in choices):
            raise ValueError(f"Каждый вариант должен быть непустой строкой до {MAX_CHOICE_LENGTH} символов.")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata должен быть объектом JSON.")
        try:
            encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
                raise ValueError("Слишком большой объект metadata.")
            metadata = json.loads(encoded)
        except (TypeError, OverflowError, RecursionError):
            raise ValueError("metadata должен содержать только данные JSON.") from None
        return Message(
            sender=self.key,
            text=text,
            created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            visibility=visibility,
            metadata=metadata,
            choices=choices,
        )

    def can_see(self, message: dict) -> bool:
        visibility = message["visibility"]
        return visibility["mode"] == "all" or (
            visibility["mode"] == "selected" and self.key in visibility["chat_ids"]
        )
