# sfusd-waitlist-scrape

Prints your child's SFUSD school-choice waitlist positions from ParentVUE, so you
can check them from a terminal instead of clicking through the portal.

It signs in to [ParentVUE](https://ca-sfu.edupoint.com/PXP2_Login_Parent.aspx),
opens **Student Info/Waitlist**, and pulls out the *Waitlist for School Year* table.

```
Waitlist for School Year 2026-27
Student: Robin Q. Example

Preference Order  School            Grade  Pathway            Status or Waitlist Position
----------------  ----------------  -----  -----------------  ---------------------------
1                 Example ES        TK     General Education  2
2                 Another ES        TK     General Education  43
3                 A Third ES        TK     General Education  7
```

## Usage

Needs [uv](https://docs.astral.sh/uv/). Dependencies are declared inline (PEP 723),
so there is nothing to install — the first run also downloads the Chromium build
Playwright drives.

```bash
uv run sfusd_waitlist.py
```

That prompts for your ParentVUE username and password. To avoid the prompt, copy
`.env.example` to `.env` (git-ignored) and fill it in:

```
PARENTVUE_USERNAME=parent@example.com
PARENTVUE_PASSWORD=…
```

`-u/--username` and `-p/--password` also work, but a password in argv shows up in
`ps` and your shell history — prefer `.env` or the prompt.

## Options

| Flag | What it does |
| --- | --- |
| `--format table\|csv\|json` | output shape (default `table`) |
| `--year 2027-28` | a different school year heading (default `2026-27`) |
| `--student NAME` | pick a child, if the account has several |
| `--headed` | show the browser instead of running headless |
| `-v` / `-q` | log every selector tried / no progress output |
| `--timeout 45` | per-step timeout, seconds |
| `--screenshot out.png` | save a full-page screenshot |
| `--dump-html page.html` | save the page HTML |

Progress goes to stderr and the table to stdout, so piping stays clean:

```bash
uv run sfusd_waitlist.py -q --format csv > waitlist.csv
```

## How it works

1. Posts the login form at `PXP2_Login_Parent.aspx` (Synergy's standard field ids,
   with text-based fallbacks).
2. Navigates to `PXP2_Student.aspx?AGU=0` — the page the left-nav
   "Student Info/Waitlist" item leads to. Clicking the nav item itself is
   unreliable, since it isn't a plain link.
3. Finds the smallest element whose text matches *Waitlist for School Year &lt;year&gt;*,
   takes the table it labels, and renders it.

The parsing half (`extract_waitlist`) is pure BeautifulSoup and takes HTML as a
string, so you can point it at a saved `--dump-html` file to debug without logging
in again.

## When it breaks

Scrapers rot when the site changes. Two things to try:

- `--dump-html page.html --screenshot page.png` shows exactly what came back.
- `--headed` watches it happen in a real browser window.

If the waitlist section moved or was renamed, the error lists the *Waitlist for
School Year* headings it did find on the page.

Unofficial and unaffiliated with SFUSD or Edupoint; it just reads your own account.
