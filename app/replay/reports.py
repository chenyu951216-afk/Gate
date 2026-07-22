import csv
import io
import json
from html import escape
from typing import Any


def export_json(job: dict[str, Any]) -> str:
    return json.dumps(job, ensure_ascii=False, indent=2, default=str)


def export_csv(job: dict[str, Any]) -> str:
    output = io.StringIO()
    rows: list[dict[str, Any]] = []
    for timepoint in job.get("results", []):
        for ranking_type, items in timepoint.get("rankings", {}).items():
            for item in items:
                rows.append({"aligned_time": timepoint.get("aligned_time"), "ranking_type": ranking_type, **{key: item.get(key) for key in ("rank", "contract", "direction", "ranking_score", "bull_score", "bear_score", "confidence", "data_completeness_pct")}})
    fields = ["aligned_time", "ranking_type", "rank", "contract", "direction", "ranking_score", "bull_score", "bear_score", "confidence", "data_completeness_pct"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_html(job: dict[str, Any]) -> str:
    sections = []
    for timepoint in job.get("results", []):
        sections.append(f"<h2>{escape(str(timepoint.get('aligned_time')))}</h2><pre>{escape(json.dumps(timepoint.get('rankings', {}), ensure_ascii=False, indent=2, default=str))}</pre>")
    return "<!doctype html><html lang='zh-Hant'><meta charset='utf-8'><title>Gate replay report</title><body>" + "".join(sections) + "</body></html>"

