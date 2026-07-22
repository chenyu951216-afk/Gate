from typing import Any

from app.notifications.chunker import chunk_message


def _number(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value):+.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _compact_item(item: dict[str, Any], icon: str) -> str:
    metrics = item.get("metrics", {})
    ticker = metrics.get("ticker", {})
    direction = "看多" if item.get("direction") == "long" else "看空"
    return (
        f"{icon} #{item.get('rank', '—')} {item.get('contract', '—')}｜{direction}｜"
        f"24h {_number(ticker.get('change_percentage'), '%', 2)}｜"
        f"分數 {_number(item.get('ranking_score'), '', 1)}｜"
        f"把握 {_number(item.get('confidence'), '%', 0)}｜"
        f"{item.get('market_state', '—')} / {item.get('signal_state', '—')}"
    )


def _section(title: str, items: list[dict[str, Any]], icon: str) -> str:
    lines = [title]
    lines.extend(_compact_item(item, icon) for item in items)
    if not items:
        lines.append("目前沒有符合全部門檻的可靠排名")
    return "\n".join(lines)


def format_scan(result: dict[str, Any]) -> list[str]:
    rankings = result.get("rankings", {})
    header = "\n".join(
        [
            "📊 Gate 30m 掃描完成",
            f"市場 {result.get('universe_total', 0)}｜分析成功 {result.get('successful_count', 0)}｜錯誤 {result.get('error_count', 0)}",
            "把握程度不等於實際勝率；以下 24h% 是價格變化，不是勝率。",
        ]
    )
    messages: list[str] = []
    for content in (
        header + "\n\n" + _section("🟣 綜合排名", rankings.get("combined", []), "⭐"),
        _section("🟢 看多排名", rankings.get("long", []), "🟢"),
        _section("🔴 看空排名", rankings.get("short", []), "🔴"),
    ):
        messages.extend(chunk_message(content))
    return messages


def format_replay_overview(job: dict[str, Any]) -> list[str]:
    diagnostics = job.get("diagnostics", {})
    return chunk_message(
        "\n".join(
            [
                "🕘 Gate 歷史重播完成",
                f"開始 {job.get('request', {}).get('start_time')}｜結束 {job.get('request', {}).get('end_time')}",
                f"時間點 {len(job.get('results', []))}｜可靠時間點 {diagnostics.get('reliable_timepoints', 0)}｜無可靠排名 {diagnostics.get('unreliable_timepoints', 0)}",
                "Discord 顯示精簡版；完整 diagnostics 請查看網頁或 export。",
            ]
        )
    )


def format_replay_timepoint(timepoint: dict[str, Any], include_all: bool) -> list[str]:
    rankings = timepoint.get("rankings", {})
    keys = ("combined", "long", "short") if include_all else ("combined",)
    content = [f"🕘 {timepoint.get('aligned_time')} 重播排名"]
    icons = {"combined": "⭐", "long": "🟢", "short": "🔴"}
    for key in keys:
        content.append(_section(key, rankings.get(key, []), icons[key]))
    return chunk_message("\n".join(content))

