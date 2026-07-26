from app.constants import MAX_DISCORD_MESSAGE_LENGTH


def chunk_message(message: str, limit: int = MAX_DISCORD_MESSAGE_LENGTH) -> list[str]:
    if len(message) <= limit:
        return [message]
    content_limit = max(1, limit - 20)
    lines = message.splitlines(keepends=True)
    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) <= content_limit:
            current += line
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > content_limit:
            chunks.append(line[:content_limit].rstrip())
            line = line[content_limit:]
        current = line
    if current.strip():
        chunks.append(current.rstrip())
    total = len(chunks)
    return [f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, start=1)]
