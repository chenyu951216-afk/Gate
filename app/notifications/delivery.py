import uuid
from datetime import datetime, timezone
from typing import Any

from app.notifications.deduplication import FingerprintDeduplicator
from app.notifications.discord import DiscordWebhook
from app.notifications.formatter import format_replay_overview, format_replay_timepoint, format_scan


class NotificationService:
    def __init__(self, settings: Any, repository: Any):
        self.repository = repository
        self.settings = settings
        scan_url = settings.scan_discord_webhook_url or settings.discord_webhook_url
        self.scan_discord = DiscordWebhook(scan_url)
        self.order_discord = DiscordWebhook(settings.order_discord_webhook_url)
        # Keep the old attribute for existing status pages and integrations.
        self.discord = self.scan_discord
        self.dedup = {
            "scan": FingerprintDeduplicator(settings.discord_cooldown_seconds),
            "order": FingerprintDeduplicator(0),
        }

    async def send_messages(
        self,
        messages: list[str],
        metadata: dict[str, Any] | None = None,
        channel: str = "scan",
        deduplicate: bool = True,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        webhook = self.order_discord if channel == "order" else self.scan_discord
        if not webhook.enabled:
            result = {"delivery_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc), "channel": channel, "status": "disabled", "message_count": 0, "metadata": metadata}
            await self.repository.save_notification(result)
            return result
        sent = 0
        error = None
        for message in messages:
            if deduplicate and not self.dedup[channel].accept(message):
                continue
            ok, error = await webhook.send(message, self.settings.discord_max_retries)
            if not ok:
                break
            sent += 1
        result = {"delivery_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc), "channel": channel, "status": "sent" if error is None else "failed", "message_count": sent, "error": error, "metadata": metadata}
        await self.repository.save_notification(result)
        return result

    async def send_scan(self, result: dict[str, Any]) -> dict[str, Any]:
        return await self.send_messages(format_scan(result), {"scan_id": result.get("scan_id")}, channel="scan")

    async def send_order(self, action: dict[str, Any]) -> dict[str, Any]:
        contract = action.get("contract") or action.get("position_key") or "UNKNOWN"
        status = action.get("status", "unknown")
        lines = [f"Bitget 下單/持倉通知｜{contract}", f"狀態：{status}"]
        for key in ("side", "size", "entry_price", "notional", "leverage", "stop_loss", "phase", "current_r"):
            if key in action:
                lines.append(f"{key}：{action[key]}")
        if action.get("code"):
            lines.append(f"風控代碼：{action['code']}")
        if action.get("error"):
            lines.append(f"錯誤：{action['error']}")
        return await self.send_messages(
            ["\n".join(lines)],
            {"contract": contract, "status": status, "action": action},
            channel="order",
            deduplicate=False,
        )

    async def send_replay(self, job: dict[str, Any], include_all: bool) -> list[dict[str, Any]]:
        deliveries = [await self.send_messages(format_replay_overview(job), {"job_id": job.get("job_id")}, channel="scan")]
        timepoints = job.get("results", [])[: self.settings.discord_max_timepoints]
        for timepoint in timepoints:
            deliveries.append(await self.send_messages(format_replay_timepoint(timepoint, include_all), {"job_id": job.get("job_id"), "timestamp": timepoint.get("aligned_time")}, channel="scan"))
        return deliveries
