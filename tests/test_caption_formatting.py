from html import unescape
from types import SimpleNamespace

from bot import (
    DB_CAPTION_MAX_UNITS,
    DB_CAPTION_PROVIDER_TEXT,
    DB_CAPTION_PROVIDER_URL,
    DB_CAPTION_SUPPORT_TEXT,
    DB_CAPTION_SUPPORT_URL,
    _database_caption_html,
    _new_database_caption,
    clean_database_caption,
)


def telegram_utf16_units(text):
    return len(str(text).encode("utf-16-le")) // 2


def test_clean_database_caption_removes_links_mentions_quotes_and_promo():
    source = (
        "> Nani's Gang Leader (2019) 720p WebRip Dual Audio "
        "[Hindi HQ Dub]+Telugu x264 ESubs @theempirebay\n"
        "https://example.com/source\n"
        "Powered By : MS Landers\n"
        "Share & Support : @somechannel"
    )

    cleaned = clean_database_caption(source)

    assert cleaned == (
        "Nani's Gang Leader (2019) 720p WebRip Dual Audio "
        "[Hindi HQ Dub]+Telugu x264 ESubs"
    )

    assert "@theempirebay" not in cleaned
    assert "http://" not in cleaned
    assert "https://" not in cleaned
    assert "t.me/" not in cleaned
    assert "Powered By" not in cleaned
    assert "Share & Support" not in cleaned
    assert not cleaned.startswith(">")


def test_clean_database_caption_keeps_movie_information_and_visible_markdown_label():
    source = (
        "Spider-Man Brand New Day (2026) 1080p FHD Quality\n"
        "Dual Audio Hindi & English\n"
        "[IMDb](https://www.imdb.com/title/example)"
    )

    cleaned = clean_database_caption(source)

    assert "Spider-Man Brand New Day (2026)" in cleaned
    assert "1080p FHD Quality" in cleaned
    assert "Dual Audio Hindi & English" in cleaned
    assert "IMDb" in cleaned
    assert "https://www.imdb.com" not in cleaned


def test_database_caption_html_makes_entire_upper_caption_bold_and_adds_clickable_footer():
    upper = (
        "Nani's Gang Leader (2019) 720p WebRip Dual Audio "
        "[Hindi HQ Dub]+Telugu x264 ESubs"
    )

    html = _database_caption_html(upper)

    assert html.startswith("<b>")
    assert "</b>\n\n<b>➤ Provided By :" in html

    assert (
        f'<a href="{DB_CAPTION_PROVIDER_URL}">'
        f"{DB_CAPTION_PROVIDER_TEXT}</a>"
    ) in html

    assert (
        f'<a href="{DB_CAPTION_SUPPORT_URL}">'
        f"{DB_CAPTION_SUPPORT_TEXT}</a>"
    ) in html

    assert html.endswith(
        f'</b>\n\n<b>➤ Share &amp; Support : '
        f'<a href="{DB_CAPTION_SUPPORT_URL}">'
        f"{DB_CAPTION_SUPPORT_TEXT}</a></b>"
    )

    # Confirm the complete upper caption is inside one bold block.
    bold_upper = html.split("</b>\n\n", 1)[0][len("<b>"):]

    assert "Nani's Gang Leader (2019)" in unescape(bold_upper)
    assert "Cinema HUB" not in unescape(bold_upper)


def test_new_database_caption_uses_filename_when_caption_is_empty():
    msg = SimpleNamespace(
        caption=None,
        text=None,
        message_id=123,
        video=None,
        audio=None,
        document=SimpleNamespace(
            file_name="Example_Movie_2026_1080p.mkv"
        ),
        file=None,
    )

    upper, html = _new_database_caption(msg)

    assert upper == "Example Movie 2026 1080p.mkv"
    assert upper in unescape(html)
    assert html.startswith(
        "<b>Example Movie 2026 1080p.mkv</b>"
    )


def test_database_caption_html_stays_within_telegram_caption_limit():
    # Emoji uses two UTF-16 code units, which is important for
    # Telegram's caption-length accounting.
    upper = ("Movie Title 🎬 " * 200).strip()

    html = _database_caption_html(upper)

    visible_upper = unescape(
        html.split("</b>", 1)[0][len("<b>"):]
    )

    assert telegram_utf16_units(visible_upper) <= DB_CAPTION_MAX_UNITS


def test_generated_footer_contains_only_the_new_branding():
    html = _database_caption_html(
        "Sample Movie 2026 720p"
    )

    assert "CINEMA HUB ❤️🎬" in html
    assert "Ꮩɪꜱɪᴏɴᴀʀʏ ☆" in html

    assert "MS Landers" not in html
    assert "Powered By" not in html


def test_html_escaping_keeps_source_text_as_text_not_markup():
    html = _database_caption_html(
        'Movie <Test> & "Special"'
    )

    assert "&lt;Test&gt;" in html
    assert "&amp;" in html
    assert "<Test>" not in html


def test_caption_cleaner_adds_one_gap_before_metadata_and_removes_join_promo():
    source = (
        "Movie Title 1080p FHD\n"
        "🔊 Audio: Hindi, English\n"
        "💬 MSubs\n"
        "Join Telegram Channel : https://t.me/example"
    )
    cleaned = clean_database_caption(source)
    assert "Movie Title 1080p FHD\n\n🔊 Audio: Hindi, English" in cleaned
    assert "Join Telegram Channel" not in cleaned
    assert "https://t.me/example" not in cleaned
