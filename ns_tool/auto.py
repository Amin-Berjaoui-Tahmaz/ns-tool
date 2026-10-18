"""Unattended monthly run, meant for cron on the 15th:

  1. Log in to Mijn NS with NS_EMAIL / NS_PASSWORD.
  2. Show last month's journey history.
  3. Select the work trips (same logic as the interactive CLI).
  4. Click Download and save the PDF to ./downloads/.
  5. Send the PDF to Telegram.

On any failure it sends a screenshot + page HTML instead, so you know to
do that month by hand.

Environment (put these in .env, see README):
  NS_EMAIL, NS_PASSWORD             Mijn NS login
  TELEGRAM_BOT_TOKEN                from @BotFather
  TELEGRAM_CHAT_ID                  your chat with that bot
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import traceback
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from .cli import open_browser, select_and_declare
from .config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger(__name__)


def month_bounds(day: date) -> tuple[date, date]:
    """First and last day of the month containing `day`."""
    next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day.replace(day=1), next_month - timedelta(days=1)


def _click_if_present(page: Page, pattern: str) -> None:
    btn = page.get_by_role("button", name=re.compile(pattern, re.I)).first
    try:
        btn.click(timeout=3000)
    except Exception:
        pass


def login(page: Page, email: str, password: str) -> None:
    page.goto("https://www.ns.nl/mijnns")
    page.wait_for_url(re.compile(r"login\.ns\.nl"), timeout=30000)
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("#loginForm [type=submit]")
    try:
        page.wait_for_url(lambda url: "login.ns.nl" not in url, timeout=30000)
    except Exception:
        raise RuntimeError("Still on the login page -- wrong password, or NS wants a captcha/2FA.")
    page.wait_for_load_state("networkidle")
    # Cookie banner: keep only the necessary cookies.
    _click_if_present(page, r"weiger|decline|reject|noodzakelijk|necessary")


PERIOD_OPTIONS = {
    "this": r"deze maand|this month",
    "last": r"vorige maand|last month|previous month",
}


def period_option(start: date, today: date) -> str:
    """Which entry of NS's period dropdown covers the month starting at `start`.

    ponytail: only "this month" / "last month" from the dropdown; older months
    need NS's "Kies handmatig" date picker, add it if backfills are ever needed.
    """
    this_month = today.replace(day=1)
    if start == this_month:
        return "this"
    if start == (this_month - timedelta(days=1)).replace(day=1):
        return "last"
    raise RuntimeError(
        f"NS's period dropdown only covers this month and last month; get {start:%B %Y} by hand."
    )


def show_period(page: Page, option: str) -> None:
    """Open Reishistorie, pick this/last month in the period dropdown, click Tonen."""
    page.get_by_role(
        "link", name=re.compile(r"journey history|reishistorie|reistransacties", re.I)
    ).first.click()
    select = page.locator("#filter-period-control")
    select.wait_for(timeout=30000)

    labels = select.locator("option").all_inner_texts()
    index = next(
        (i for i, label in enumerate(labels) if re.search(PERIOD_OPTIONS[option], label, re.I)),
        None,
    )
    if index is None:
        raise RuntimeError(f"No '{option} month' entry in the period dropdown: {labels}")
    select.select_option(index=index)

    page.locator("mns-transaction-filter nes-button").click()


def download_statement(page: Page, download_dir: Path) -> Path:
    button = page.locator("mns-transaction-download-bar nes-button").filter(
        has_text=re.compile("download", re.I)
    )
    with page.expect_download(timeout=60000) as info:
        button.click()
    target = download_dir / info.value.suggested_filename
    info.value.save_as(target)
    return target


def _telegram(method: str, fields: dict[str, str], document: Path | None = None) -> None:
    """POST one multipart/form-data call to the Telegram Bot API."""
    boundary = uuid.uuid4().hex
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in fields.items()
    )
    if document is not None:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
            f'filename="{document.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode() + document.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    # Errors raise without the URL, which holds the token.
    with urllib.request.urlopen(request, timeout=60) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError(f"Telegram {method} failed")


def send_document(path: Path) -> None:
    _telegram("sendDocument", {"chat_id": os.environ["TELEGRAM_CHAT_ID"]}, path)


def notify(title: str, body: str, attachments: list[Path]) -> None:
    """Send a message, then each attachment, to TELEGRAM_CHAT_ID."""
    # Telegram caps a message at 4096 characters.
    _telegram(
        "sendMessage",
        {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": f"{title}\n\n{body}"[:4096]},
    )
    for path in attachments:
        send_document(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download last month's NS commute statement.")
    parser.add_argument("--month", help="YYYY-MM to fetch instead of last month.")
    parser.add_argument("--headed", action="store_true", help="Show the browser (for testing).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    today = date.today()
    start = (
        date.fromisoformat(args.month + "-01")
        if args.month
        else (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    )
    label = start.strftime("%B %Y")

    download_dir = Path("downloads").resolve()
    download_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser, context = open_browser(p, headless=not args.headed)
        page = context.new_page()
        try:
            login(page, os.environ["NS_EMAIL"], os.environ["NS_PASSWORD"])
            show_period(page, period_option(start, today))
            trips = select_and_declare(
                page, config.work_stations, config.max_gap_minutes, config.home_stations
            )
            if not trips:
                raise RuntimeError(f"No work trips found for {label}.")
            pdf = download_statement(page, download_dir)
        except Exception:
            shot = download_dir / "ns-tool-error.png"
            html = download_dir / "ns-tool-error.html"
            shot.unlink(missing_ok=True)
            html.unlink(missing_ok=True)
            try:
                page.screenshot(path=shot, full_page=True)
                html.write_text(page.content(), encoding="utf-8")
            except Exception:
                logger.exception("Couldn't capture the failing page")
            notify(
                f"NS commute statement {label}: FAILED, do it by hand",
                f"Page: {page.url}\n\n{traceback.format_exc()}",
                [f for f in (shot, html) if f.exists()],
            )
            raise
        finally:
            browser.close()

    send_document(pdf)
    print(f"Sent {pdf.name} to Telegram")


if __name__ == "__main__":
    main()
