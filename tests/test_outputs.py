import json
from pathlib import Path

REPORT = Path("/app/report.json")


def load_report():
    """Load /app/report.json and confirm it exists and is valid JSON."""
    assert REPORT.exists(), "/app/report.json was not created."
    with REPORT.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_total_requests():
    """Success criterion 1: total_requests equals the number of request lines (6)."""
    report = load_report()
    assert report["total_requests"] == 6, report.get("total_requests")


def test_unique_ips():
    """Success criterion 2: unique_ips equals the count of distinct client IPs (3)."""
    report = load_report()
    assert report["unique_ips"] == 3, report.get("unique_ips")


def test_top_path():
    """Success criterion 3: top_path is the most-requested path (/index.html)."""
    report = load_report()
    assert report["top_path"] == "/index.html", report.get("top_path")
