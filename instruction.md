A raw product event log is available at `/app/events.csv`. Build an **honest weekly-cohort retention chart** from it and save it as an SVG to `/app/retention.svg`.

The CSV has a header and three columns:

- `user_id` — an opaque string id.
- `event_ts` — the event time as a UTC ISO-8601 string ending in `Z` (e.g. `2025-09-17T19:00:00Z`).
- `event_type` — one of `signup`, `activity`, or decoy types such as `page_view` / `logout` that you must ignore.

## Definitions (follow these exactly)

- **Weeks.** Bucket every timestamp by its **ISO-8601, Monday-start week in the `America/New_York` timezone**. Convert each UTC timestamp to New York local time (this must be DST-aware) before taking its week. A week is labelled by its Monday's date, `YYYY-MM-DD`.
- **Cohort.** A user's cohort is the week of their **earliest `signup` event**. If a user has several `signup` rows, use the earliest. **Users that have activity but no `signup` event are excluded entirely** (from both cohort membership and every count).
- **Cohort size.** The number of distinct users in the cohort (i.e. distinct users whose earliest signup falls in that week).
- **Retention[cohort, k]** for weeks-since-signup `k ≥ 1` = (number of **distinct** cohort users with at least one `activity` event in the week `signup_week + k`) ÷ (cohort size). Count each user at most once per week. **Retention at k = 0 is 100% by construction.**
- **Right-censoring.** The **observed span** is the closed interval from the earliest to the latest `event_ts` across **all rows in the file, regardless of `event_type`**. A week is **complete** if both its Monday 00:00:00 and its Sunday 23:59:59 (New York local) fall inside that span; the **last complete week** is the latest such week. Only plot a point `(cohort, k)` when `signup_week + k` is on or before the last complete week. Do **not** plot, impute, or zero-fill weeks that have not fully happened yet.
- **Cap.** After censoring, plot only `k` in `0..12`; drop any `k > 12`.
- **Blended curve.** In addition to the per-cohort curves, draw one **blended** curve. At each `k` it is the **cohort-size-weighted mean** of `Retention[cohort, k]` taken over **only the cohorts still observed at that `k`** (cohorts whose data is censored at `k` drop out of that week's average). Blended at `k = 0` is 100%.

## Output chart contract (the verifier reads the SVG geometry)

Render with **matplotlib** and save as SVG to `/app/retention.svg`. The chart must satisfy:

- **Axes.** X-axis = weeks since signup with **integer ticks 0, 1, …, 12**; Y-axis = retention in percent with ticks at least at 0, 20, 40, 60, 80, 100.
- **One line per cohort**, drawn in **chronological order** of signup week, coloured with matplotlib's **`tab10`** palette in that order (so each cohort gets a distinct colour), each with a **distinct marker shape** and a **marker at every integer-week data point**.
- **Blended line** drawn as a **dashed black** line with **no markers**.
- **Legend.** One entry per cohort labelled with its signup-week Monday date (`YYYY-MM-DD`), in chronological order, followed by the blended entry labelled exactly **`Blended`** as the **last** entry.
- **Readable text.** Emit text as real characters, not glyph outlines — set matplotlib's rcParam `svg.fonttype` to `'none'` before saving so tick and legend labels are readable text in the SVG.

Write the chart only to the absolute path `/app/retention.svg`. `matplotlib`, `pandas`, and timezone data are already installed.
