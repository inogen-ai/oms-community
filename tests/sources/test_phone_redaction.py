"""The phone rule redacts numbers and leaves dates, ranges and path segments alone."""
import pytest

from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext


def scrub(text: str) -> str:
    context = ExecutionContext(user_input="", agent_raw_output="", user_correction=text)
    return RegexSanitiser().sanitise(context).context.user_correction


@pytest.mark.parametrize("number", ["+44 7700 900123", "+44 20 7946 0958", "07700 900123", "555-123-4567",
                                    "(555) 123-4567", "0161-4960000", "1234567890", "4111 1111 1111 1111"])
def test_numbers_are_still_redacted(number):
    # The candidate pattern starts at a digit, so a leading "(" is kept. That is
    # existing behaviour, and the check is on the placeholder and the removed digits.
    result = scrub(f"Call {number} today.")
    assert "<PHONE_1>" in result
    assert number[-4:] not in result


@pytest.mark.parametrize("text", ["In session 2025-10-03, we found it.", "Logged at 2025-10-03T09:00:00.",
                                  "Wait 300000-600000 ms.", "Seen 2019-2024.",
                                  "Path /brainstorm/12345-1706000000/content here."])
def test_dates_ranges_and_path_segments_are_kept(text):
    assert scrub(text) == text


def test_a_range_broken_across_a_line_is_kept():
    text = "`timeout_ms` 300000-600000\n  (5-10 minutes)."
    assert scrub(text) == text


def test_two_numbers_on_one_line_get_distinct_placeholders():
    assert scrub("call 07700 900123 or 07700 900456") == "call <PHONE_1> or <PHONE_2>"
