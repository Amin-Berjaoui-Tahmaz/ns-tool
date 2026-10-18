# ns-tool

<p align="center"> <img src="docs/banner.svg" alt="ns-tool banner" width="640"> </p>

Mijn NS work-trip selector. Picks out your work-related trips, selects
them as declarations and gets you the statement PDF. Runs two ways:

- **Automatic** (`ns_tool.auto`): runs on a server via cron on the 15th,
  logs in, downloads last month's statement and sends you the PDF on
  Telegram.
- **Interactive** (`ns_tool.cli`): you log in and pick the period, the
  tool checks the boxes, and you review and click Download yourself.

## Setup

```bash
git clone https://github.com/Amin-Berjaoui-Tahmaz/ns-tool.git
cd ns-tool
uv sync --extra dev
uv run playwright install chromium   # one-time, downloads the browser binary
uv run -m ns_tool.cli
```

First run with no `config.yaml` walks you through a couple of prompts
(work stations, transfer gap, home stations) and saves your answers.

To change your stations later, edit `config.yaml` directly (or delete it
to be asked again):

```yaml
work_stations:
  - Sliedrecht
  - Ketelhaven
max_gap_minutes: 20
home_stations:
  - Rijnhaven
  - Rotterdam Zuid
```

## Automatic monthly run (server)

On a Linux server:

```bash
git clone https://github.com/Amin-Berjaoui-Tahmaz/ns-tool.git ~/ns-tool
cd ~/ns-tool
uv sync
uv run playwright install --with-deps chromium
cp .env.example .env && chmod 600 .env   # fill in NS login + Telegram bot
# copy your config.yaml over too
```

For Telegram:
1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and put
   the token it gives you in `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
   `"chat":{"id":...}` into `TELEGRAM_CHAT_ID`.

If the token ever leaks, someone can act as that bot but has no access
to your own Telegram account. Revoke it with `/revoke` in BotFather.

Try it once by hand (`--month YYYY-MM` also works, but only for this month or last month: those are the entries in NS's period dropdown):

```bash
set -a && . ./.env && set +a && uv run -m ns_tool.auto
```

Then schedule it with `crontab -e` (09:00 on the 15th, fetches the
previous month):

```
0 9 15 * * cd ~/ns-tool && set -a && . ./.env && set +a && ~/.local/bin/uv run -m ns_tool.auto >> auto.log 2>&1
```

The Telegram message lists every selected trip so you can review the
PDF before forwarding it. If anything fails (wrong login, NS changed its
page), you get a "FAILED" message instead, with a screenshot and the page
HTML attached.

On a desktop, `--headed` shows the browser while it runs.

## Interactive run

```bash
uv run -m ns_tool.cli
uv run -m ns_tool.cli --work-station Sliedrecht --work-station Ketelhaven --max-gap 15
```

1. In the browser, log in (English or Dutch both work).
2. Go to "Journey history & transactions", set
   the period you want, and click Show.
3. Click "Scan + select trips" in the overlay button (bottom-right of the
   page) -- or press Enter in the terminal, whichever's easier.

   <img src="docs/button.png" alt="Diagram showing the overlay button's position on the page, clear of the site's own Download button" width="520">

4. The script scans the page, groups same-day trips into journeys (see
   below), checks the boxes for any journey touching a work station, and
   switches the bottom bar to "X declarations".
5. Review on the page, then click Download yourself whenever you're
   ready -- it's saved to `./downloads/` automatically.
6. Change the period in the browser, click/press again to repeat. Close
   the browser or press Ctrl+C when done.

## How trips are selected

A real commute is several trip segments back to back. With
`home_stations` set, each day is split into **home-to-home journeys**: a
journey starts on a segment leaving home and ends on one arriving home.
Any journey that touches a work station is selected in full, so legs like
"Rijnhaven -> Zuidplein" come along even though they never mention work:

```
 Rijnhaven ─► Zuidplein ─► Sliedrecht ··· Sliedrecht ─► Ridderkerk   Zuidplein ─► Rijnhaven
 └───────────────────────────── one journey, all selected ─────────────────────────────┘
          starts leaving home                                    ends arriving home
```

Station names and transfer gaps between segments don't matter, so a
bus that checks you out at one stop and in at another still counts.

Days that never pass a home station (or with no `home_stations` set) fall
back to chaining: segments link when one's arrival roughly matches the
next one's departure within `max_gap_minutes`.

See `select_work_chains` in `ns_tool/trip_model.py` for the implementation.
