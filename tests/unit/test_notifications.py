from app.notifications.chunker import chunk_message
from app.notifications.deduplication import FingerprintDeduplicator
from app.notifications.formatter import format_scan

def test_discord_chunking_preserves_parts():
    chunks = chunk_message("x" * 5000); assert len(chunks) >= 3; assert all(len(chunk) <= 2000 for chunk in chunks); assert "1/" in chunks[0]

def test_fingerprint_deduplication():
    dedup = FingerprintDeduplicator(900); assert dedup.accept("same"); assert not dedup.accept("same")

def test_formatter_includes_all_sections():
    result = {"scan_id":"a","finished_at":"now","universe_total":3,"successful_count":1,"error_count":0,"rankings":{"combined":[],"long":[],"short":[]}}; text = "\n".join(format_scan(result)); assert "綜合排名" in text and "看多排名" in text and "看空排名" in text
