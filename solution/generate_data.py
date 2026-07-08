#!/usr/bin/env python3
"""Deterministic generator for the cohort-retention task.

Build-time only. This file lives under solution/ and is NOT copied into the
agent's environment image. It produces two artifacts:

  * environment/events.csv   -- the raw event log the agent sees
  * tests/ground_truth.json  -- the expected chart values the verifier checks

Ground truth is derived directly from an explicit DESIGN (cohort sizes and
per-week distinct-active counts), so it is independent of any parsing pipeline.
The generator then re-derives the retention matrix from the emitted CSV using a
correct America/New_York, Monday-start pipeline and asserts it matches the
design (self-consistency), and asserts a >=3pp separation margin between the
correct values and three canonical wrong methods.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVENTS_CSV = ROOT / "environment" / "events.csv"
GROUND_TRUTH = ROOT / "tests" / "ground_truth.json"

# --------------------------------------------------------------------------
# Calendar: Monday-start weeks in NY local time.
# --------------------------------------------------------------------------
# First Monday on/after 2025-09-01.
_start = date(2025, 9, 1)
BASE_MONDAY = _start + timedelta(days=(7 - _start.weekday()) % 7)  # a Monday


def week_monday(index: int) -> date:
    """Monday date (NY local) of the week `index` weeks after BASE_MONDAY."""
    return BASE_MONDAY + timedelta(weeks=index)


# The DST fall-back in the US is Sunday 2025-11-02 (clocks 02:00 -> 01:00).
# Verify it lands on a Sunday inside one of our weeks (used as a trap below).
assert date(2025, 11, 2).weekday() == 6, "2025-11-02 should be a Sunday"

# --------------------------------------------------------------------------
# DESIGN: cohorts keyed by signup-week index. Each cohort has a size and a list
# of distinct-active counts for k = 0, 1, 2, ...  (k = weeks since signup).
# k=0 count MUST equal the cohort size (100% by construction).
# Cohort labels are the Monday date of the signup week.
# --------------------------------------------------------------------------
DESIGN = {
    0:  {"size": 20, "counts": [20, 15, 12, 11, 9, 6, 8, 8, 7, 6, 6, 5]},
    2:  {"size": 25, "counts": [25, 20, 18, 15, 13, 10, 10, 9, 8, 7]},
    4:  {"size": 10, "counts": [10, 7, 6, 5, 4, 2, 4, 3]},
    6:  {"size": 40, "counts": [40, 30, 26, 22, 30, 24]},
    8:  {"size": 8,  "counts": [8, 6, 5, 4]},
    11: {"size": 5,  "counts": [5]},  # entirely censored (only k=0 observed)
}

# The observed span's last complete week. Cohort week 11 == last complete week,
# so cohort 11 is entirely censored. We set max(event_ts) inside week 12 so that
# week 11 is the last week with all 7 NY-local days inside the span.
LAST_COMPLETE_INDEX = 11

for idx, d in DESIGN.items():
    assert d["counts"][0] == d["size"], f"cohort {idx} k0 must equal size"
    assert idx + (len(d["counts"]) - 1) <= LAST_COMPLETE_INDEX, (
        f"cohort {idx} extends past last complete week"
    )


# --------------------------------------------------------------------------
# Ground truth (from the design, independent of the CSV).
# --------------------------------------------------------------------------
def cohort_points(idx: int) -> list[list[float]]:
    d = DESIGN[idx]
    pts = []
    for k, cnt in enumerate(d["counts"]):
        wk = idx + k
        if wk > LAST_COMPLETE_INDEX or k > 12:
            break
        pts.append([k, 100.0 * cnt / d["size"]])
    return pts


def blended_points() -> list[list[float]]:
    """Size-weighted mean over cohorts still OBSERVED at each k.

    weighted_mean(pct) = 100 * sum(active_count) / sum(cohort_size) over
    cohorts whose max observed k >= this k.
    """
    pts = []
    for k in range(0, 13):
        num = 0
        den = 0
        for idx, d in DESIGN.items():
            max_k = min(len(d["counts"]) - 1, LAST_COMPLETE_INDEX - idx, 12)
            if k <= max_k:
                num += d["counts"][k]
                den += d["size"]
        if den == 0:
            break
        pts.append([k, 100.0 * num / den])
    return pts


def build_ground_truth() -> dict:
    cohorts = []
    for idx in sorted(DESIGN):
        cohorts.append(
            {
                "label": week_monday(idx).isoformat(),
                "week_index": idx,
                "size": DESIGN[idx]["size"],
                "points": cohort_points(idx),
            }
        )
    return {
        "cohorts": cohorts,
        "blended": {"label": "Blended", "points": blended_points()},
        "entirely_censored_label": week_monday(11).isoformat(),
        "x_range": [-0.5, 12.5],
        "y_range": [0.0, 105.0],
        "y_tol_pp": 1.0,
        "x_tol": 0.15,
    }


# --------------------------------------------------------------------------
# Event synthesis.
# --------------------------------------------------------------------------
def ny_iso_utc(d: date, hour: int, minute: int, fold: int = 0) -> str:
    """A NY-local wall time -> UTC ISO-8601 'Z' string."""
    dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=NY, fold=fold)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Events:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, user_id: str, ts_utc: str, event_type: str):
        self.rows.append(
            {"user_id": user_id, "event_ts": ts_utc, "event_type": event_type}
        )


def uid(cohort_idx: int, n: int) -> str:
    return f"u{cohort_idx:02d}{n:03d}"


# Boundary/DST discriminators keyed by (cohort_idx, k, user_n): the user's sole
# activity that week is a late-Sunday NY event, so under correct NY bucketing it
# stays in week (idx+k), but any UTC/fixed-offset bucketing pushes it into the
# NEXT week -- and because these users are NOT active in that next week, the
# distinct counts diverge. (0,2,11): a plain week boundary in EDT.
# (0,8,6): the Sunday is 2025-11-02, so this also exercises the DST fall-back
# (23:30 NY is EST -> 04:30Z the next day).
BOUNDARY = {
    (0, 2, 11): (23, 45),
    (0, 8, 6): (23, 30),
}


def synthesize() -> Events:
    ev = Events()

    for idx, d in DESIGN.items():
        signup_monday = week_monday(idx)
        size = d["size"]

        # --- signup events: one per cohort member, in the signup week. ---
        for n in range(size):
            u = uid(idx, n)
            # Signups on Tuesday, hour varies by user (mid-week, safe).
            sd = signup_monday + timedelta(days=1)
            ev.add(u, ny_iso_utc(sd, 8 + (n % 8), (n * 7) % 60), "signup")

        # --- activity events realizing the distinct-active counts. ---
        for k, cnt in enumerate(d["counts"]):
            wk_monday = week_monday(idx + k)
            for n in range(cnt):
                u = uid(idx, n)
                bnd = BOUNDARY.get((idx, k, n))
                if bnd is not None:
                    # Late Sunday NY (end of this week) -> UTC = next week.
                    sun = wk_monday + timedelta(days=6)
                    ev.add(u, ny_iso_utc(sun, bnd[0], bnd[1]), "activity")
                else:
                    # Active users: Wednesday, varied hour (mid-week, safe).
                    ad = wk_monday + timedelta(days=2)
                    ev.add(u, ny_iso_utc(ad, 9 + (n % 9), (n * 13) % 60), "activity")

        # --- decoy events (ignored types) for some members every week. ---
        for k in range(len(d["counts"])):
            wk_monday = week_monday(idx + k)
            u = uid(idx, 0)
            ev.add(u, ny_iso_utc(wk_monday + timedelta(days=3), 11, 5), "page_view")
            ev.add(u, ny_iso_utc(wk_monday + timedelta(days=4), 12, 5), "logout")

    return ev


def add_traps(ev: Events) -> None:
    """Add complications that must NOT change the correct distinct-active
    counts, but that break naive pipelines."""

    # (1) Duplicate signup events: user u00000 (cohort 0) also has an EARLIER
    # and a LATER signup row. Cohort must be the EARLIEST signup's week (still
    # week 0), so this must not move the cohort. Add a same-week duplicate and
    # a later (week 3) duplicate to tempt "last signup" logic.
    u = uid(0, 0)
    ev.add(u, ny_iso_utc(week_monday(0) + timedelta(days=1), 6, 0), "signup")
    ev.add(u, ny_iso_utc(week_monday(3) + timedelta(days=1), 9, 0), "signup")

    # (2) Duplicate ACTIVITY events (same user, same week) for cohort 6, k=1.
    # Distinct count stays 30; an event-count method would inflate this vertex.
    wk = week_monday(6 + 1) + timedelta(days=2)
    for n in range(5):  # 5 already-active users get an extra activity event
        ev.add(uid(6, n), ny_iso_utc(wk, 14, 10 + n), "activity")

    # (3) Activity-without-signup users: have activity but NO signup event.
    # They must be excluded from numerators and cohort sizes entirely.
    for j in range(4):
        gu = f"ghost{j:02d}"
        for wkidx in (1, 3, 5):
            ev.add(gu, ny_iso_utc(week_monday(wkidx) + timedelta(days=2), 10, j), "activity")

    # (4) Timezone / DST discriminators are integrated into synthesize() via the
    # BOUNDARY table: chosen users' sole activity that week is a late-Sunday NY
    # event, so naive UTC/fixed-offset bucketing moves them into the next week
    # (where they are not otherwise active), changing distinct counts.

    # (0) End-span anchor: a single decoy event inside week 12 (a partial week)
    # so the observed span reaches past Sunday of week 11 -> week 11 becomes the
    # last COMPLETE week (all 7 NY days present), while week 12 stays incomplete.
    # Uses a decoy type + a signup-less user, so it changes neither cohorts nor
    # activity, only the span. Week 12 Monday = 2025-11-24; pick Wed 2025-11-26.
    ev.add("ghost00", ny_iso_utc(week_monday(12) + timedelta(days=2), 12, 0), "page_view")


# --------------------------------------------------------------------------
# Self-consistency: recompute retention from the emitted CSV with a correct
# NY-tz, Monday-start, distinct-per-week pipeline and compare to the design.
# --------------------------------------------------------------------------
def recompute_from_rows(rows: list[dict]):
    from collections import defaultdict

    # NY-local Monday for each event.
    def monday_of(ts_utc: str) -> date:
        dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        loc = dt.astimezone(NY)
        return loc.date() - timedelta(days=loc.date().weekday())

    signups = defaultdict(list)   # user -> [monday...]
    activity = defaultdict(set)   # user -> {monday...}
    for r in rows:
        m = monday_of(r["event_ts"])
        if r["event_type"] == "signup":
            signups[r["user_id"]].append(m)
        elif r["event_type"] == "activity":
            activity[r["user_id"]].add(m)

    cohort_of = {u: min(ms) for u, ms in signups.items()}  # earliest signup week
    # cohort_size = distinct users per cohort week
    from collections import Counter
    size = Counter(cohort_of.values())

    # active[cohort_monday][k] = set of users active that week
    active = defaultdict(lambda: defaultdict(set))
    for u, weeks in activity.items():
        if u not in cohort_of:
            continue  # activity-without-signup -> excluded
        c = cohort_of[u]
        for w in weeks:
            k = (w - c).days // 7
            if k >= 0:
                active[c][k].add(u)

    # Derive last complete week from the observed span over ALL rows, exactly as
    # the reference solution does, and confirm it matches the intended index.
    dts = [
        datetime.strptime(r["event_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for r in rows
    ]
    span_min_ny = min(dts).astimezone(NY)
    span_max_ny = max(dts).astimezone(NY)

    def week_complete(mon: date) -> bool:
        start = datetime(mon.year, mon.month, mon.day, tzinfo=NY)
        end = start + timedelta(days=7) - timedelta(seconds=1)
        return start >= span_min_ny and end <= span_max_ny

    all_weeks = sorted({monday_of(r["event_ts"]) for r in rows})
    last_complete = max(w for w in all_weeks if week_complete(w))
    assert last_complete == week_monday(LAST_COMPLETE_INDEX), (
        f"derived last_complete {last_complete} != intended {week_monday(LAST_COMPLETE_INDEX)}"
    )
    result = {}
    for c in sorted(size):
        cidx = (c - BASE_MONDAY).days // 7
        pts = []
        kmax = (last_complete - c).days // 7
        for k in range(0, min(kmax, 12) + 1):
            n = len(active[c].get(k, set())) if k > 0 else size[c]
            # k=0 by construction == size
            n = size[c] if k == 0 else len(active[c].get(k, set()))
            pts.append((k, 100.0 * n / size[c]))
        result[cidx] = {"size": size[c], "points": pts, "label": c.isoformat()}
    return result


def assert_self_consistent(gt: dict, rows: list[dict]) -> None:
    rec = recompute_from_rows(rows)
    design_idx = set(DESIGN)
    assert set(rec) == design_idx, (
        f"cohort set mismatch: recomputed {sorted(rec)} vs design {sorted(design_idx)}"
    )
    for c in gt["cohorts"]:
        idx = c["week_index"]
        r = rec[idx]
        assert r["label"] == c["label"], (idx, r["label"], c["label"])
        assert r["size"] == c["size"], (idx, r["size"], c["size"])
        exp = {k: v for k, v in c["points"]}
        got = {k: v for k, v in r["points"]}
        assert set(exp) == set(got), (idx, sorted(exp), sorted(got))
        for k in exp:
            assert abs(exp[k] - got[k]) < 1e-9, (idx, k, exp[k], got[k])


def assert_margins(gt: dict) -> None:
    """Each canonical wrong method must deviate >=3pp from the correct value on
    at least one checked vertex, so a +/-1pp band discriminates method."""
    MARGIN = 3.0

    # Wrong method A: unweighted mean of observed cohort pcts (blended).
    correct_blend = {k: v for k, v in gt["blended"]["points"]}
    unweighted = {}
    for k in range(0, 13):
        vals = []
        for c in gt["cohorts"]:
            pmap = {kk: vv for kk, vv in c["points"]}
            if k in pmap:
                vals.append(pmap[k])
        if vals:
            unweighted[k] = sum(vals) / len(vals)
    sep_a = max(abs(correct_blend[k] - unweighted[k]) for k in unweighted)
    assert sep_a >= MARGIN, f"unweighted-blend separation too small: {sep_a:.2f}pp"

    # Wrong method B: full-set blend counting censored cohorts as 0% (no
    # censoring) -- 100*sum(active)/sum(ALL cohort sizes).
    total_size = sum(DESIGN[i]["size"] for i in DESIGN)
    fullset = {}
    for k in range(0, 13):
        num = 0
        any_obs = False
        for idx, d in DESIGN.items():
            max_k = min(len(d["counts"]) - 1, LAST_COMPLETE_INDEX - idx, 12)
            if k <= max_k:
                num += d["counts"][k]
                any_obs = True
        if any_obs:
            fullset[k] = 100.0 * num / total_size
    sep_b = max(abs(correct_blend[k] - fullset[k]) for k in fullset)
    assert sep_b >= MARGIN, f"fullset-blend separation too small: {sep_b:.2f}pp"

    # Wrong method C: event-count (not distinct) normalization on cohort 6, k=1.
    # design distinct=30 -> 75%; event-count = 30 + 5 duplicates = 35 -> 87.5%.
    ec_pct = 100.0 * 35 / 40
    correct_pct = 100.0 * 30 / 40
    sep_c = abs(ec_pct - correct_pct)
    assert sep_c >= MARGIN, f"event-count separation too small: {sep_c:.2f}pp"

    return {"unweighted_pp": sep_a, "fullset_pp": sep_b, "eventcount_pp": sep_c}


# --------------------------------------------------------------------------
def main() -> None:
    gt = build_ground_truth()
    ev = synthesize()
    add_traps(ev)

    # Sort rows by timestamp for realism, then stably by user.
    rows = sorted(ev.rows, key=lambda r: (r["event_ts"], r["user_id"], r["event_type"]))

    assert_self_consistent(gt, rows)
    seps = assert_margins(gt)

    EVENTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["user_id", "event_ts", "event_type"])
        w.writeheader()
        w.writerows(rows)

    GROUND_TRUTH.parent.mkdir(parents=True, exist_ok=True)
    with GROUND_TRUTH.open("w", encoding="utf-8") as f:
        json.dump(gt, f, indent=2)

    print(f"wrote {EVENTS_CSV} ({len(rows)} rows)")
    print(f"wrote {GROUND_TRUTH}")
    print(f"base Monday: {BASE_MONDAY}  last complete: {week_monday(LAST_COMPLETE_INDEX)}")
    print("cohorts:", [c["label"] for c in gt["cohorts"]])
    print("entirely censored:", gt["entirely_censored_label"])
    print("separation margins (pp):", {k: round(v, 2) for k, v in seps.items()})


if __name__ == "__main__":
    main()
