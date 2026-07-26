import pytest
from app.exceptions import TimeAlignmentError
from app.replay.timeline import build_timeline

def test_replay_timeline_uses_taipei_and_includes_end():
    points = build_timeline("2026-06-09T10:00", "2026-06-09T12:00", "Asia/Taipei", 30); assert len(points) == 5; assert points[0].hour == 2; assert points[-1].hour == 4

def test_non_boundary_can_align_down():
    points = build_timeline("2026-06-09T10:10", "2026-06-09T11:10", "Asia/Taipei", 30, "down"); assert points[0].minute == 0; assert points[-1].minute == 0

def test_invalid_strict_alignment_raises():
    with pytest.raises(TimeAlignmentError): build_timeline("2026-06-09T10:10", "2026-06-09T11:10", "Asia/Taipei", 30, "strict")

