An Apache-style access log is available at `/app/access.log`. Parse it and write a JSON report to `/app/report.json`.

The report must be a single, valid JSON object containing exactly these three keys and no others:

1. `total_requests` — an integer equal to the number of request lines in the log, where a request line is any non-blank line. Blank lines are ignored.
2. `unique_ips` — an integer equal to the number of distinct client IP addresses. The client IP is the first whitespace-separated field on each request line.
3. `top_path` — a string: the request path that appears in the most request lines. The path is the second token inside the quoted request (for example, `"GET /index.html HTTP/1.1"` has the path `/index.html`). If two or more paths tie for the highest count, any one of the tied paths is acceptable.

Write the report only to the absolute path `/app/report.json`. Do not create any other files.
