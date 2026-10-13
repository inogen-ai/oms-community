"""The phone rule redacts numbers and leaves dates, ranges and path segments alone."""
import pytest

from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext


def _scrub(sanitiser: RegexSanitiser, text: str) -> str:
    context = ExecutionContext(user_input="", agent_raw_output="", user_correction=text)
    return sanitiser.sanitise(context).context.user_correction


def scrub(text: str) -> str:
    return _scrub(RegexSanitiser(), text)


def scrub_source(text: str) -> str:
    """As a skill package is checked: a bare Unix time there is a timestamp, not a number."""
    return _scrub(RegexSanitiser(keep_unix_times=True), text)


@pytest.mark.parametrize("number", ["+44 7700 900123", "+44 20 7946 0958", "07700 900123", "555-123-4567",
                                    "(555) 123-4567", "0161-4960000", "1234567890", "4111 1111 1111 1111"])
def test_numbers_are_still_redacted(number):
    # The candidate pattern starts at a digit, so a leading "(" is kept. That is
    # existing behaviour, and the check is on the placeholder and the removed digits.
    result = scrub(f"Call {number} today.")
    assert "<PHONE_1>" in result
    assert number[-4:] not in result


@pytest.mark.parametrize("text", ["In session 2025-10-03, we found it.", "Logged at 2025-10-03T09:00:00.",
                                  "Logged at 2025-10-03 09:00:00.", "Started 2025-10-03 9:05, done.",
                                  "Wait 300000-600000 ms.", "Seen 2019-2024.",
                                  "Path /brainstorm/12345-1706000000/content here."])
def test_dates_ranges_and_path_segments_are_kept(text):
    assert scrub(text) == text


def test_a_range_broken_across_a_line_is_kept():
    text = "`timeout_ms` 300000-600000\n  (5-10 minutes)."
    assert scrub(text) == text


def test_two_numbers_on_one_line_get_distinct_placeholders():
    assert scrub("call 07700 900123 or 07700 900456") == "call <PHONE_1> or <PHONE_2>"


def test_a_number_after_a_date_is_still_redacted():
    # Only a time after the date makes it a date-time; digits that are not a time
    # are not excused by the date before them.
    assert "900123" not in scrub("On 2025-10-03 07700 900123 rang.")


@pytest.mark.parametrize("text", ['<svg viewBox="0 0 12 12">', '<svg viewBox="0 0 18 18" fill="none">'])
def test_short_digit_runs_are_kept(text):
    # Fewer than 7 digits is never a phone number, whatever is being checked.
    assert scrub(text) == text
    assert scrub_source(text) == text


@pytest.mark.parametrize("text", ["Started at 1706000000 seconds.", "expires 1999999999 then",
                                  "first seen 1500000000."])
def test_unix_times_are_kept_in_source_text(text):
    # In a skill package a bare ten-digit run from 1500000000 to 1999999999 is a
    # Unix time in seconds (July 2017 to May 2033).
    assert scrub_source(text) == text


@pytest.mark.parametrize(("text", "expected"), [
    ("Call 1932345678 today.", "Call <PHONE_1> today."), ("Call 1541234567 today.", "Call <PHONE_1> today."),
    ("Started at 1706000000 seconds.", "Started at <PHONE_1> seconds.")])
def test_the_default_sanitiser_redacts_numbers_in_the_unix_time_range(text, expected):
    # Real numbers fall in that range: a Brazilian landline in area codes 15 to 19
    # written bare, or an Argentine local mobile after "15". A correction is
    # personal data, so the default keeps redacting them.
    assert scrub(text) == expected


@pytest.mark.parametrize("number", ["1499999999", "2000000000", "+1706000000", "17060000001", "0170 600 0000"])
def test_numbers_outside_the_unix_time_shape_are_still_redacted(number):
    assert "<PHONE_1>" in scrub(f"Call {number} today.")
    assert "<PHONE_1>" in scrub_source(f"Call {number} today.")
