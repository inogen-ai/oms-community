"""The email rule needs an alphabetic top-level domain, so a package version is not an address."""
import time

import pytest

from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import ExecutionContext


def scrub(text: str) -> str:
    context = ExecutionContext(user_input="", agent_raw_output="", user_correction=text)
    return RegexSanitiser().sanitise(context).context.user_correction


@pytest.mark.parametrize("address", ["jane.doe@example.com", "a.b+c@x.co.uk", "jane@company.com",
                                     "ops@sub.domain.example", "user@host.xn--p1ai"])
def test_addresses_are_redacted(address):
    result = scrub(f"Write to {address} today.")
    assert "<EMAIL_1>" in result and address not in result


@pytest.mark.parametrize("text", ['<script src="https://unpkg.com/react@18.3.1/umd/react.development.js">',
                                  "npm install pkg@1.2.3", "pin left-pad@1.3.0 and react-dom@18.3.1"])
def test_package_versions_are_kept(text):
    assert scrub(text) == text


@pytest.mark.parametrize("text", ["Write to ops@example.org, today.", "(ops@example.org)", "ops@example.org."])
def test_addresses_next_to_punctuation_are_redacted(text):
    result = scrub(text)
    assert "<EMAIL_1>" in result and "ops@example.org" not in result


@pytest.mark.parametrize(("text", "expected"), [
    ("Write to jane@company.com.2 people", "Write to <EMAIL_1>.2 people"),
    ("mail jane@company.com.2. Escalate", "mail <EMAIL_1>.2. Escalate"),
    ("See jane@company.com.¹", "See <EMAIL_1>.¹")])
def test_an_address_followed_by_a_dot_and_a_digit_is_redacted(text, expected):
    # The tail is not part of the address: it is trimmed, and the address before it is redacted.
    assert scrub(text) == expected


@pytest.mark.parametrize("text", ["a@" + "a." * 20000 + "1", "a@" + "a.1" * 20000, "a." * 50000,
                                  "a@" + "a-" * 50000],
                         ids=["dotted-labels", "dotted-digits", "dots-no-at", "hyphens-after-at"])
def test_long_dotted_tails_scrub_in_linear_time(text):
    start = time.perf_counter()
    scrub(text)
    assert time.perf_counter() - start < 1.0
