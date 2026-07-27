
import httpx


class DiscordWebhook:
    def __init__(self, webhook_url: str | None, timeout: float = 15.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, message: str, retries: int = 4) -> tuple[bool, str | None]:
        if not self.webhook_url:
            return False, "disabled"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max(1, retries)):
                try:
                    response = await client.post(self.webhook_url, json={"content": message})
                    if response.status_code == 429:
                        try:
                            retry_after = float(response.json().get("retry_after", 1))
                        except (ValueError, TypeError):
                            retry_after = 1.0
                        import asyncio
                        await asyncio.sleep(min(30.0, retry_after))
                        continue
                    if response.status_code >= 500:
                        if attempt + 1 < retries:
                            import asyncio
                            await asyncio.sleep(min(8.0, 0.5 * 2**attempt))
                            continue
                    response.raise_for_status()
                    return True, None
                except (httpx.HTTPError, OSError) as exc:
                    if attempt + 1 == retries:
                        return False, type(exc).__name__
                    import asyncio
                    await asyncio.sleep(min(8.0, 0.5 * 2**attempt))
        return False, "delivery_failed"

