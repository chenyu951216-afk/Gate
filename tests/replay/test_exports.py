from app.replay.reports import export_csv, export_html, export_json

def test_replay_exports():
    item = {"rank":1,"contract":"BTC_USDT","direction":"long","ranking_score":80,"bull_score":80,"bear_score":20,"confidence":75,"data_completeness_pct":90}; job = {"job_id":"j","results":[{"aligned_time":"2026-01-01T00:00:00+00:00","rankings":{"combined":[item],"long":[],"short":[]}}]}
    assert '"job_id": "j"' in export_json(job); assert "BTC_USDT" in export_csv(job); assert "Gate replay report" in export_html(job)

