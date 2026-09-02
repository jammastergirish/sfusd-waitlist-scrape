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

## Watching for changes

`watch_waitlist.sh` polls every five minutes and sends a desktop notification
only when your top choice actually moves — not on every check.

```bash
./watch_waitlist.sh              # every 5 minutes until you ctrl-c
./watch_waitlist.sh --once       # a single check, for cron or launchd
./watch_waitlist.sh --pref 2     # watch the second choice instead
./watch_waitlist.sh --interval 900
./watch_waitlist.sh --timeout 120  # the portal is having a slow day
```

The portal's response time varies a lot — a scrape that takes 7 seconds one hour
can take 20 the next, and sometimes a request just hangs — so the watcher gives
each check 90 seconds by default, rather than the 45 the scraper uses
interactively, and retries a failed check three times with a widening gap
(5s, then 15s, then 45s). `--retries` and `RETRY_DELAY` / `RETRY_FACTOR` tune it.

```
[2026-09-02 11:04:08] attempt 1 failed, retrying in 5s: error: timed out — Page.goto: Timeout 90000ms exceeded.
[2026-09-02 11:04:20] recovered on attempt 2
[2026-09-02 11:04:20] NOTIFY SFUSD waitlist — Example ES Position: 2
```

Rejected credentials are not a wobble, so those fail immediately without
retrying — repeatedly posting a bad password is how you get an account locked.

The last seen position is kept in `.waitlist_position` (git-ignored) as
`School|Position`, so restarting the watcher will not re-announce a move you have
already been told about. Delete that file to re-baseline.

```
[2026-09-02 10:29:11] baseline: choice 1 is Example ES at position 3
[2026-09-02 10:34:11] no change: choice 1 is Example ES at position 3
[2026-09-02 10:39:11] NOTIFY SFUSD waitlist — Example ES Position: 2
```

Notifications go through `terminal-notifier` if you have it, otherwise
`osascript` on macOS or `notify-send` on Linux. Set `NTFY_TOPIC` in `.env` to
also push to your phone via [ntfy.sh](https://ntfy.sh) — install the app and
subscribe to the same topic. The topic name is the only thing keeping it private,
so pick something nobody would guess. It also notifies if the top row
changes school or drops off the list entirely — an offer, or a withdrawn choice.

Because it runs unattended it cannot answer a password prompt: it needs `.env` or
exported `PARENTVUE_*` variables, and says so rather than hanging. A failed check
(network, portal down) is logged and skipped without touching the saved position.

For something that survives a reboot, wrap `--once` in a launchd job or a cron
entry instead of leaving the loop running.

## Setting it up on another Mac

Start to finish, assuming nothing is installed yet. Everything happens in
Terminal — open it with ⌘-Space, type `Terminal`, press return.

**1. Install uv**, which runs the script and fetches what it needs:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close Terminal and open it again afterwards. (If you already have Homebrew,
`brew install uv` does the same thing.)

**2. Download this repo** into your home folder:

```bash
git clone https://github.com/jammastergirish/sfusd-waitlist-scrape.git ~/sfusd-waitlist-scrape
cd ~/sfusd-waitlist-scrape
```

macOS may offer to install developer tools the first time you run `git` — accept,
then run the command again. If you would rather not, the green **Code** button on
the GitHub page has a **Download ZIP** option; unzip it and `cd` into the folder.

**3. Save your ParentVUE login** so it does not ask every time. Replace the two
values with your own, keeping the single quotes:

```bash
printf 'PARENTVUE_USERNAME=%s\nPARENTVUE_PASSWORD=%s\n' 'you@example.com' 'your-password' > .env && chmod 600 .env
```

The single quotes matter: a password containing `!` will otherwise confuse the
shell. This file stays on your Mac and is never committed.

**4. Check it works.** The first run takes a minute or two — it downloads a
private copy of Chrome to read the page with:

```bash
uv run sfusd_waitlist.py
```

You should see your waitlist table printed.

**5. Let it send notifications.** Run this once, which makes macOS aware of the
notifier:

```bash
osascript -e 'display notification "Setting up" with title "SFUSD waitlist"'
```

Then open **System Settings → Notifications**, find **Script Editor** in the list,
and turn on *Allow notifications*. This step is easy to miss: without it macOS
silently throws the notifications away and you will never see one.

**6. Start watching:**

```bash
./watch_waitlist.sh
```

Leave that Terminal window open. It checks every five minutes and stays quiet
unless your first-choice school moves, in which case a notification appears
saying, for example, `McKinley ES Position: 2`. Press **control-C** to stop it.

It only checks while that window is open and the Mac is awake — closing the lid
pauses it, and it picks up again when you reopen it.

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
