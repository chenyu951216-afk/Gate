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
        reason_map = {
            "limit_order_open": "限價進場單已掛出",
            "submitted": "進場成交並已交由持倉管理",
            "skipped_existing_position": "同幣同方向持倉已存在，刷新趨勢時效",
            "skipped_existing_open_order": "同幣同方向進場掛單已存在",
            "skipped_total_position_limit": "全帳戶持倉數已達上限",
            "skipped_market_driver_limit": "大盤驅動幣持倉已達上限",
            "skipped_contract_unavailable": "Bitget 找不到完全相符的可交易合約",
            "skipped_reversal_only_signal": "訊號只允許管理或反轉既有持倉",
            "rejected_risk": "交易計畫未通過必要風險檢查",
            "failed": "交易所或系統執行失敗",
            "time_decay_closed": "持倉超時且趨勢未產生，時間止損退出",
            "time_decay_grace": "持倉到期但趨勢仍健康，給予有限延長",
            "skipped_trading_disabled": "自動交易目前未啟用",
            "skipped_trading_paused": "交易控制目前暫停",
            "skipped_reentry_cooldown": "近期剛退出同方向，暫停重複進場以避免手續費與噪音磨損",
        }
        lines = [
            f"📌 Bitget 交易決策｜{contract}",
            f"結果：{status}",
            f"原因：{reason_map.get(status, action.get('reason') or '交易生命週期更新')}",
        ]
        for key in (
            "side", "ranking_score", "confidence", "market_state", "signal_state",
            "size", "entry_price", "entry_limit_price", "notional",
            "leverage", "stop_loss", "phase", "current_r", "holding_hours",
            "time_remaining_hours", "trend_confirmations",
        ):
            if key in action:
                lines.append(f"{key}：{action[key]}")
        sizing = action.get("position_sizing")
        if isinstance(sizing, dict):
            for key in ("execution_quality_score", "actual_risk_pct_equity", "promoted_from_risk_size"):
                if key in sizing:
                    lines.append(f"{key}：{sizing[key]}")
        if action.get("code"):
            lines.append(f"代碼：{action['code']}")
        if action.get("error"):
            lines.append(f"詳細：{action['error']}")
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
