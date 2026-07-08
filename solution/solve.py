#!/usr/bin/env python3
"""Reference solution: build honest cohort-retention curves from the raw event
log and render them to /app/retention.svg following the encoding contract in
the instructions.

Pipeline:
  parse CSV -> localize to America/New_York (DST-aware) -> Monday-start ISO
  weeks -> cohort = earliest signup week (drop signup-less users) -> deduped
  (user, week) activity -> retention matrix normalized by cohort size ->
  right-censoring at the last complete week (+ drop k>12) -> observed-only
  size-weighted blend -> matplotlib SVG.
"""

from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams["svg.fonttype"] = "none"  # readable <text> in the SVG
import matplotlib.pyplot as plt

NY = ZoneInfo("America/New_York")
EVENTS = os.environ.get("EVENTS_CSV", "/app/events.csv")
OUT = os.environ.get("RETENTION_SVG", "/app/retention.svg")

MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*", "h", "<"]


def monday_of(ts_utc: str) -> date:
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    loc = dt.astimezone(NY)
    d = loc.date()
    return d - timedelta(days=d.weekday())  # Monday-start week


def main() -> None:
    signups: dict[str, list[date]] = defaultdict(list)
    activity: dict[str, set[date]] = defaultdict(set)
    all_mondays: list[date] = []
    span_min = span_max = None

    with open(EVENTS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row["event_ts"]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            span_min = dt if span_min is None else min(span_min, dt)
            span_max = dt if span_max is None else max(span_max, dt)
            m = monday_of(ts)
            all_mondays.append(m)
            et = row["event_type"]
            if et == "signup":
                signups[row["user_id"]].append(m)
            elif et == "activity":
                activity[row["user_id"]].add(m)

    # Cohort = earliest signup week; drop users without a signup.
    cohort_of = {u: min(ms) for u, ms in signups.items()}
    size = Counter(cohort_of.values())

    # Distinct active users per (cohort, week).
    active = defaultdict(lambda: defaultdict(set))
    for u, weeks in activity.items():
        c = cohort_of.get(u)
        if c is None:
            continue
        for w in weeks:
            k = (w - c).days // 7
            if k >= 0:
                active[c][k].add(u)

    # Last complete week: latest Monday whose Mon 00:00 .. Sun 23:59:59 (NY)
    # both lie inside the observed span (over ALL rows).
    span_min_ny = span_min.astimezone(NY)
    span_max_ny = span_max.astimezone(NY)

    def week_complete(mon: date) -> bool:
        start = datetime(mon.year, mon.month, mon.day, 0, 0, 0, tzinfo=NY)
        end = start + timedelta(days=7) - timedelta(seconds=1)
        return start >= span_min_ny and end <= span_max_ny

    all_weeks = sorted(set(all_mondays))
    complete = [w for w in all_weeks if week_complete(w)]
    last_complete = max(complete)

    cohorts = sorted(size)  # chronological
    fig, ax = plt.subplots(figsize=(9, 6))
    cmap = plt.get_cmap("tab10")

    # Retention curves, censored at last complete week and capped at k=12.
    matrix = {}  # cohort -> list[(k, pct)]
    for i, c in enumerate(cohorts):
        kmax = (last_complete - c).days // 7
        pts = []
        for k in range(0, min(kmax, 12) + 1):
            n = size[c] if k == 0 else len(active[c].get(k, set()))
            pts.append((k, 100.0 * n / size[c]))
        matrix[c] = pts
        xs = [k for k, _ in pts]
        ys = [v for _, v in pts]
        ax.plot(
            xs,
            ys,
            color=cmap(i),
            marker=MARKERS[i % len(MARKERS)],
            markersize=7,
            linewidth=1.8,
            label=c.isoformat(),
        )

    # Observed-only, size-weighted blended curve.
    blended = []
    for k in range(0, 13):
        num = 0
        den = 0
        for c in cohorts:
            pts = dict(matrix[c])
            if k in pts:
                num += pts[k] / 100.0 * size[c]
                den += size[c]
        if den:
            blended.append((k, 100.0 * num / den))
    bx = [k for k, _ in blended]
    by = [v for _, v in blended]
    ax.plot(bx, by, color="black", linestyle="--", linewidth=2.2, label="Blended")

    ax.set_xlim(-0.5, 12.5)
    ax.set_ylim(0, 105)
    ax.set_xticks(range(0, 13))
    ax.set_yticks(range(0, 101, 20))
    ax.set_xlabel("Weeks since signup")
    ax.set_ylabel("Retention (%)")
    ax.set_title("Weekly cohort retention")
    ax.legend(title="Cohort (signup week)", loc="upper right")
    ax.grid(True, alpha=0.3)

    fig.savefig(OUT, format="svg")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
