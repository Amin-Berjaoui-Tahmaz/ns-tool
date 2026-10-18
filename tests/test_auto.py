import re
from datetime import date

import pytest

from ns_tool.auto import PERIOD_OPTIONS, month_bounds, notify, period_option
from ns_tool.ns_dom import DATE_RE


def test_month_bounds():
    assert month_bounds(date(2026, 9, 30)) == (date(2026, 9, 1), date(2026, 9, 30))
    assert month_bounds(date(2024, 2, 1)) == (date(2024, 2, 1), date(2024, 2, 29))
    assert month_bounds(date(2026, 12, 15)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_period_option_only_this_or_last_month():
    today = date(2026, 10, 18)
    assert period_option(date(2026, 10, 1), today) == "this"
    assert period_option(date(2026, 9, 1), today) == "last"
    assert period_option(date(2025, 12, 1), date(2026, 1, 15)) == "last"
    with pytest.raises(RuntimeError, match="by hand"):
        period_option(date(2026, 8, 1), today)


def test_period_dropdown_labels_match_real_page():
    # Option texts from the real Reishistorie page.
    labels = [" Laatste 7 dagen ", " Deze maand ", " Vorige maand ", " Afgelopen jaar ",
              " Afgelopen 1,5 jaar ", " Kies handmatig "]
    hits = {k: [l.strip() for l in labels if re.search(p, l, re.I)] for k, p in PERIOD_OPTIONS.items()}
    assert hits == {"this": ["Deze maand"], "last": ["Vorige maand"]}


def test_date_headings_english_and_dutch():
    assert re.search(DATE_RE, "Tuesday September 1, 2026", re.I)
    assert re.search(DATE_RE, "dinsdag 1 september 2026", re.I)
    assert not re.search(DATE_RE, "08:24 Rotterdam, Zuidplein", re.I)


def test_notify_sends_message_then_pdf(monkeypatch, tmp_path):
    sent = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        sent.append(request)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    notify("NS commute statement September 2026", "x" * 5000, [pdf])

    message, document = sent
    assert message.full_url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert b'name="chat_id"\r\n\r\n42\r\n' in message.data
    assert b"x" * 5000 not in message.data  # trimmed to Telegram's 4096 cap
    assert document.full_url.endswith("/sendDocument")
    assert b'filename="statement.pdf"' in document.data
    assert b"%PDF-1.4" in document.data
