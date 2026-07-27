from app.coinglass.client import _heatmap_features, _history_features


def test_coinglass_heatmap_accepts_official_nested_liq_map_shape():
    result = _heatmap_features(
        {
            "code": "0",
            "data": {
                "data": {
                    "liqMapV2": {
                        "100": [[100, 5000, None, None]],
                        "110": [[110, 8000, None, None]],
                    }
                }
            },
        },
        105,
    )
    assert result["cluster_count"] == 2
    assert result["nearest_below"]["price"] == 100
    assert result["nearest_above"]["price"] == 110


def test_coinglass_history_normalizes_seconds_to_milliseconds():
    result = _history_features(
        {
            "data": [{
                "time": 1_700_000_000,
                "aggregated_long_liquidation_usd": "10",
                "aggregated_short_liquidation_usd": "20",
            }]
        }
    )
    assert result["rows"][0]["time"] == 1_700_000_000_000
    assert result["dominant"] == "short_liquidation_dominant"
