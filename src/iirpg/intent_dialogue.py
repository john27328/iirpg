"""Bounded, persistent conversations scoped to a single unexecuted intent."""

MAX_MESSAGES = 16
MAX_TEXT_BYTES = 6000


def append_message(intent: dict, role: str, text: str, proposal: dict | None = None) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError('Напишите сообщение до 1000 символов.')
    if 'messages' not in intent:
        intent['messages'] = [{'role': 'user', 'text': intent['text']}]
        if role == 'user' and intent.get('analysis'):
            intent['messages'].append({'role':'assistant', 'text':intent['analysis']['summary']})
    history = intent['messages']
    message_limit = MAX_MESSAGES - 1 if role == 'user' else MAX_MESSAGES
    byte_limit = MAX_TEXT_BYTES - 1200 if role == 'user' else MAX_TEXT_BYTES
    if len(history) >= message_limit or sum(len(m['text'].encode()) for m in history) + len(text.encode()) > byte_limit:
        raise ValueError('Достигнут лимит обсуждения намерения. Создайте новое действие.')
    message = {'role': role, 'text': text.strip()}
    if proposal:
        message['proposal'] = {'effect': proposal['effect'], **proposal['command']}
    history.append(message)
