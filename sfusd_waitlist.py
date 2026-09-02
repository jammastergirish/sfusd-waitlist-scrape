#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "playwright>=1.49",
#     "beautifulsoup4>=4.13",
#     "lxml>=5.3",
# ]
# ///
"""Pull the "Waitlist for School Year <year>" table out of SFUSD ParentVUE (Synergy).

Logs in at ca-sfu.edupoint.com, opens the Student Info/Waitlist page,
and prints the waitlist table as an aligned table, CSV, or JSON. Progress is
logged to stderr so stdout stays pipeable.

    uv run sfusd_waitlist.py                       # prompts for credentials
    uv run sfusd_waitlist.py -u parent@example.com # prompts for password only
    uv run sfusd_waitlist.py --format json --year 2026-27

Credentials are read from --username/--password, then the environment
(PARENTVUE_USERNAME / PARENTVUE_PASSWORD, incl. a local .env), then a prompt.
Prefer the env/prompt path: a password in argv is visible in `ps` and shell history.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOGIN_URL = (
    "https://ca-sfu.edupoint.com/PXP2_Login_Parent.aspx"
    "?Logout=1&regenerateSessionId=true"
)

USERNAME_SELECTORS = (
    "#ctl00_MainContent_username",
    "input[id*='username' i]",
    "input[name*='username' i]",
    "input[type='email']",
    "form input[type='text']",
)
PASSWORD_SELECTORS = (
    "#ctl00_MainContent_password",
    "input[id*='password' i]",
    "input[name*='password' i]",
    "input[type='password']",
)
SUBMIT_SELECTORS = (
    "#ctl00_MainContent_Submit1",
    "button#ctl00_MainContent_Submit1",
    "input[type='submit']",
    "button[type='submit']",
    "button:has-text('Login')",
)
LOGIN_ERROR_SELECTORS = (
    "#ctl00_MainContent_ERROR_MESSAGE",
    ".validation-summary-errors",
    ".alert-danger",
    "[id*='error' i]",
)
STUDENT_URL = "https://ca-sfu.edupoint.com/PXP2_Student.aspx?AGU=0"

DASH = "[-‐‑‒–—]"  # ASCII hyphen plus the unicode dashes Synergy renders
WAITLIST_HEADING = r"Waitlist\s*for\s*School\s*Year"

_VERBOSITY = 1
_STARTED = time.monotonic()


def log(message: str, level: int = 1) -> None:
    """Progress goes to stderr; stdout stays clean for --format csv/json."""
    if _VERBOSITY >= level:
        print(f"[{time.monotonic() - _STARTED:6.1f}s] {message}", file=sys.stderr, flush=True)


class ScrapeError(RuntimeError):
    """Anything that stops us getting to the table."""


@dataclass
class WaitlistTable:
    title: str
    headers: list[str]
    rows: list[list[str]]
    student: str | None = None


# --------------------------------------------------------------------------- #
# HTML parsing (kept free of Playwright so it can be tested on saved HTML)
# --------------------------------------------------------------------------- #


def _pad(row: list[str], width: int) -> list[str]:
    return row + [""] * (width - len(row))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def year_pattern(year: str) -> re.Pattern[str]:
    """"2026-27" -> regex tolerant of spacing and any dash character."""
    parts = [re.escape(p.strip()) for p in re.split(DASH, year) if p.strip()]
    tail = rf"\s*{DASH}\s*".join(parts)
    return re.compile(rf"{WAITLIST_HEADING}\s*{tail}", re.I)


ANY_WAITLIST = re.compile(rf"{WAITLIST_HEADING}[^\n<]{{0,20}}", re.I)


def find_heading(soup: BeautifulSoup, pattern: re.Pattern[str]) -> Tag | None:
    """Smallest element whose own text matches the pattern."""
    best: Tag | None = None
    best_size = 10**9
    for el in soup.find_all(True):
        text = _norm(el.get_text(" ", strip=True))
        if not text or len(text) > 300 or not pattern.search(text):
            continue
        if len(text) < best_size:
            best, best_size = el, len(text)
    return best


def _unwrap(table: Tag) -> Tag:
    """Descend through single-cell layout tables to the real data table."""
    while True:
        rows = table.find_all("tr", recursive=False) or table.find_all("tr")
        cells = [c for tr in rows for c in tr.find_all(["td", "th"], recursive=False)]
        if len(rows) == 1 and len(cells) == 1:
            inner = cells[0].find("table")
            if inner is not None:
                table = inner
                continue
        return table


def table_for_heading(heading: Tag) -> Tag | None:
    """The data table the heading labels: either it wraps the heading, or follows it."""
    for ancestor in heading.parents:
        if ancestor.name == "table":
            if len(ancestor.find_all("tr")) > 1:
                return _unwrap(ancestor)
            break
    following = heading.find_next("table")
    return _unwrap(following) if following is not None else None


def table_to_rows(
    table: Tag, caption: re.Pattern[str] | None = None
) -> tuple[list[str], list[list[str]]]:
    headers: list[str] = []
    body: list[list[str]] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:  # rows of a nested table
            continue
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        values = [_norm(c.get_text(" ", strip=True)) for c in cells]
        if not any(values):
            continue
        if caption and not headers and not body and caption.search(" ".join(values)):
            continue  # the section title, rendered as a row of the table itself
        is_header = bool(cells) and all(c.name == "th" for c in cells)
        if is_header and not headers and not body:
            headers = values
        else:
            body.append(values)
    return headers, body





def extract_waitlist(html: str, year: str) -> WaitlistTable:
    soup = BeautifulSoup(html, "lxml")
    log(f"parsing page: {len(html):,} bytes, {len(soup.find_all('table'))} tables", 2)
    pattern = year_pattern(year)
    heading = find_heading(soup, pattern)
    if heading is None:
        seen = sorted({_norm(m) for m in ANY_WAITLIST.findall(soup.get_text(" "))})
        detail = f" Waitlist sections on the page: {seen}." if seen else ""
        raise ScrapeError(
            f"No 'Waitlist for School Year {year}' section on the page.{detail} "
            "Re-run with --dump-html page.html to inspect what was returned."
        )
    table = table_for_heading(heading)
    if table is None:
        raise ScrapeError(
            f"Found the heading {_norm(heading.get_text(' ', strip=True))!r} "
            "but no table under it. Re-run with --dump-html page.html."
        )
    headers, rows = table_to_rows(table, pattern)
    return WaitlistTable(
        title=_norm(heading.get_text(" ", strip=True)),
        headers=headers,
        rows=rows,
        student=find_student_name(soup),
    )


def find_student_name(soup: BeautifulSoup) -> str | None:
    for el in soup.find_all(["td", "div", "span", "th"]):
        text = _norm(el.get_text(" ", strip=True))
        m = re.match(r"^Student Name\s+(.+)$", text)
        if m and len(m.group(1)) < 80:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Browser driving
# --------------------------------------------------------------------------- #


def _first_visible(page, selectors: tuple[str, ...], what: str, timeout: float):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=timeout)
            log(f"matched {what} with {selector!r}", 2)
            return locator
        except PlaywrightTimeout:
            log(f"no match for {what} with {selector!r}", 2)
    raise ScrapeError(f"Could not find {what} on {page.url} (tried: {', '.join(selectors)})")


def _login_error(page) -> str | None:
    for selector in LOGIN_ERROR_SELECTORS:
        nodes = page.locator(selector)
        for i in range(nodes.count()):
            node = nodes.nth(i)
            try:
                if node.is_visible() and (text := _norm(node.inner_text())):
                    return text
            except PlaywrightError:
                continue
    return None


def login(page, username: str, password: str, timeout: float) -> None:
    log(f"opening {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
    log(f"login page loaded: {page.title()!r}")
    _first_visible(page, USERNAME_SELECTORS, "the username field", 15_000).fill(username)
    log(f"filled username: {username}")
    _first_visible(page, PASSWORD_SELECTORS, "the password field", 15_000).fill(password)
    log("filled password (not logged)")
    _first_visible(page, SUBMIT_SELECTORS, "the login button", 15_000).click()
    log("submitted the login form, waiting for redirect…")
    try:
        page.wait_for_url(lambda url: "login_parent" not in url.lower(), timeout=timeout)
    except PlaywrightTimeout:
        raise ScrapeError(f"Login failed: {_login_error(page) or 'still on the login page'}") from None
    page.wait_for_load_state("networkidle", timeout=timeout)
    log(f"signed in → {page.url}")
    greeting = page.locator("text=/Good (morning|afternoon|evening)/i").first
    if greeting.count():
        log(_norm(greeting.inner_text()))


def select_student(page, student: str, timeout: float) -> None:
    """Pick a child when the account has more than one. No-op if no picker is shown."""
    log(f"selecting student {student!r}")
    for selector in ("#PXP2_StudentPicker select", "select[id*='student' i]"):
        picker = page.locator(selector).first
        if picker.count():
            picker.select_option(label=re.compile(student, re.I))
            page.wait_for_load_state("networkidle", timeout=timeout)
            log(f"picked {student!r} from {selector!r}")
            return
    link = page.get_by_role("link", name=re.compile(re.escape(student), re.I)).first
    if link.count():
        link.click()
        page.wait_for_load_state("networkidle", timeout=timeout)
        log(f"clicked the {student!r} link")
        return
    log("no student picker on the page — single student account")


def open_student_info(page, timeout: float) -> None:
    """Go straight to Student Info/Waitlist.

    The left-nav item is not a plain link, so clicking it is unreliable; this is
    the page it leads to.
    """
    log(f"opening {STUDENT_URL}")
    page.goto(STUDENT_URL, wait_until="domcontentloaded", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    log(f"student info page loaded → {page.url}")
    try:  # the waitlist sits below the fold; give lazy content a chance to render
        page.wait_for_selector(f"text=/{WAITLIST_HEADING}/i", timeout=15_000)
        log("waitlist section is present")
    except PlaywrightTimeout:
        log("no waitlist heading yet — scrolling to force render")
    page.mouse.wheel(0, 20_000)
    page.wait_for_timeout(500)


def _install_chromium() -> None:
    log("downloading the Chromium build Playwright needs (one time)…")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def fetch_waitlist(
    username: str,
    password: str,
    *,
    year: str = "2026-27",
    student: str | None = None,
    headed: bool = False,
    timeout: float = 45.0,
    screenshot: str | Path | None = None,
    dump_html: str | Path | None = None,
) -> WaitlistTable:
    """Log in, open Student Info/Waitlist, and return the waitlist table."""
    timeout_ms = timeout * 1000
    with sync_playwright() as p:
        log(f"launching chromium ({'headed' if headed else 'headless'})")
        try:
            browser = p.chromium.launch(headless=not headed)
        except PlaywrightError as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            _install_chromium()
            browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 1200})
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def save_artifacts() -> None:
            """Best effort, on the way out — these exist to debug a failed run."""
            try:
                if dump_html:
                    Path(dump_html).write_text(page.content(), encoding="utf-8")
                    log(f"saved page HTML → {dump_html}")
                if screenshot:
                    page.screenshot(path=str(screenshot), full_page=True)
                    log(f"saved screenshot → {screenshot}")
            except PlaywrightError as exc:
                log(f"could not save debug artifacts: {_norm(str(exc))[:120]}")

        try:
            login(page, username, password, timeout_ms)
            if student:
                select_student(page, student, timeout_ms)
            open_student_info(page, timeout_ms)
            table = extract_waitlist(page.content(), year)
            log(f"found {table.title!r}: {len(table.rows)} row(s)")
            return table
        finally:
            save_artifacts()
            context.close()
            browser.close()
            log("browser closed")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def render_text(table: WaitlistTable) -> str:
    grid = ([table.headers] if table.headers else []) + table.rows
    if not grid:
        return f"{table.title}\n(no rows)"
    ncols = max(len(r) for r in grid)
    grid = [_pad(r, ncols) for r in grid]
    widths = [max(len(r[i]) for r in grid) for i in range(ncols)]
    lines = [table.title]
    if table.student:
        lines.append(f"Student: {table.student}")
    lines.append("")
    for i, row in enumerate(grid):
        lines.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(row)).rstrip())
        if table.headers and i == 0:
            lines.append("  ".join("-" * widths[j] for j in range(ncols)))
    return "\n".join(lines)


def write_csv(table: WaitlistTable) -> None:
    writer = csv.writer(sys.stdout, lineterminator="\n")
    if table.headers:
        writer.writerow(table.headers)
    writer.writerows(table.rows)


def render_json(table: WaitlistTable) -> str:
    if table.headers:
        rows: list = [dict(zip(table.headers, _pad(r, len(table.headers)))) for r in table.rows]
    else:
        rows = table.rows
    return json.dumps(
        {"title": table.title, "student": table.student, "headers": table.headers, "rows": rows},
        indent=2,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    global _VERBOSITY
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(
        description="Print the SFUSD ParentVUE 'Waitlist for School Year' table.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-u", "--username", default=os.environ.get("PARENTVUE_USERNAME"))
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("PARENTVUE_PASSWORD"),
        help="prefer PARENTVUE_PASSWORD or the prompt; argv is visible to other processes",
    )
    parser.add_argument("--year", default="2026-27", help="school year in the section heading")
    parser.add_argument("--student", help="name to pick when the account has several children")
    parser.add_argument("--format", choices=("table", "csv", "json"), default="table")
    parser.add_argument("--headed", action="store_true", help="show the browser (debugging)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every selector tried")
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    parser.add_argument("--timeout", type=float, default=45.0, help="per-step timeout in seconds")
    parser.add_argument("--screenshot", help="save a full-page screenshot here")
    parser.add_argument("--dump-html", help="save the Student Info page HTML here")
    args = parser.parse_args(argv)
    _VERBOSITY = 0 if args.quiet else 2 if args.verbose else 1

    username = args.username or input("ParentVUE username: ").strip()
    password = args.password or getpass.getpass("ParentVUE password: ")
    if not username or not password:
        parser.error("a username and password are required")

    try:
        table = fetch_waitlist(
            username,
            password,
            year=args.year,
            student=args.student,
            headed=args.headed,
            timeout=args.timeout,
            screenshot=args.screenshot,
            dump_html=args.dump_html,
        )
    except ScrapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PlaywrightTimeout as exc:
        print(f"error: timed out — {_norm(str(exc))[:200]}", file=sys.stderr)
        return 1

    if args.format == "csv":
        write_csv(table)
    elif args.format == "json":
        print(render_json(table))
    else:
        print(render_text(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
