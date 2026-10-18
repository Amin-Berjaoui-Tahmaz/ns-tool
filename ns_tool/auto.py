"""Unattended monthly run, meant for cron on the 15th:

  1. Log in to Mijn NS with NS_EMAIL / NS_PASSWORD.
  2. Show last month's journey history.
  3. Select the work trips (same logic as the interactive CLI).
  4. Click Download and save the PDF to ./downloads/.
  5. Send the PDF to Telegram, with the selected trips listed for review.

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

from .cli import format_plan, open_browser, select_and_declare
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


def show_period(page: Page, start: date, end: date) -> None:
    """Open Journey history and show start..end.

    ponytail: selectors are best guesses written without seeing the logged-in
    page; if they miss, the failure message's screenshot + HTML says what to fix.
    """
    page.get_by_role(
        "link", name=re.compile(r"journey history|reishistorie|reistransacties", re.I)
    ).first.click()
    page.wait_for_load_state("networkidle")

    dates = page.locator("input[type=date]")
    if dates.count() >= 2:
        fmt = "%Y-%m-%d"
    else:
        dates = page.locator("input[placeholder*='dd' i], input[name*='date' i], input[id*='date' i]")
        fmt = "%d-%m-%Y"
    if dates.count() < 2:
        raise RuntimeError("Couldn't find the period's start/end date fields.")
    dates.nth(0).fill(start.strftime(fmt))
    dates.nth(1).fill(end.strftime(fmt))

    page.get_by_role("button", name=re.compile(r"^\s*(show|toon|tonen)\b", re.I)).first.click()


def download_statement(page: Page, download_dir: Path) -> Path:
    button = page.locator("button, a").filter(has_text=re.compile("download", re.I)).last
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


def notify(title: str, body: str, attachments: list[Path]) -> None:
    """Send a message, then each attachment, to TELEGRAM_CHAT_ID."""
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    # Telegram caps a message at 4096 characters.
    _telegram("sendMessage", {"chat_id": chat_id, "text": f"{title}\n\n{body}"[:4096]})
    for path in attachments:
        _telegram("sendDocument", {"chat_id": chat_id}, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download last month's NS commute statement.")
    parser.add_argument("--month", help="YYYY-MM to fetch instead of last month.")
    parser.add_argument("--headed", action="store_true", help="Show the browser (for testing).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    start, end = month_bounds(
        date.fromisoformat(args.month + "-01")
        if args.month
        else date.today().replace(day=1) - timedelta(days=1)
    )
    label = start.strftime("%B %Y")

    download_dir = Path("downloads").resolve()
    download_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser, context = open_browser(p, headless=not args.headed)
        page = context.new_page()
        try:
            login(page, os.environ["NS_EMAIL"], os.environ["NS_PASSWORD"])
            show_period(page, start, end)
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

    notify(
        f"NS commute statement {label}",
        format_plan(trips, config.work_stations),
        [pdf],
    )
    print(f"Sent {pdf.name} to Telegram")


if __name__ == "__main__":
    main()
