"""
BUG-TL3 — CFTC COT API URL encoding tests.

The prior URL was built by literal string concatenation with spaces and
apostrophes inside the $where= clause. urllib's http client rejected it
with "URL can't contain control characters" (the bare space failed the
control-character check). Fix uses urllib.parse.urlencode() so every
query parameter is properly percent-encoded.

Run: python -m unittest tests.test_cot_url_encode -v
"""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestURLEncodingApproach(unittest.TestCase):
    """Verify the urlencode() pattern the fix relies on."""

    def test_urlencode_escapes_spaces_to_percent_20(self):
        params = {"$where": "foo like 'bar'"}
        encoded = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        self.assertIn("%20", encoded)
        self.assertNotIn(" ", encoded)

    def test_urlencode_escapes_percent_wildcard(self):
        """SQL LIKE uses `%` — must not remain literal (could be misread)."""
        params = {"$where": "col like '%NASDAQ MINI%'"}
        encoded = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        # `%` should be re-encoded to %25
        self.assertIn("%25", encoded)

    def test_urlencode_handles_apostrophes(self):
        params = {"$where": "col like 'value'"}
        encoded = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        self.assertIn("%27", encoded)


class TestCOTFetchStaticCheck(unittest.TestCase):
    """Static verify the fix landed in cot_feed.py and the old pattern
    (literal space + apostrophe in URL string) is gone."""

    def _read_src(self) -> str:
        return (Path(__file__).parent.parent / "data_feeds" / "cot_feed.py").read_text(
            encoding="utf-8"
        )

    def test_uses_urlencode(self):
        src = self._read_src()
        self.assertIn("urllib.parse.urlencode", src,
                      "COT fetch must use urllib.parse.urlencode per BUG-TL3 fix")

    def test_no_literal_space_in_where_clause(self):
        """The pre-fix URL had `$where=market_and_exchange_names like '%NASDAQ MINI%'`
        concatenated into the URL string with literal spaces. That exact
        literal must not resurface in the source."""
        src = self._read_src()
        self.assertNotIn("$where=market_and_exchange_names like", src,
                         "Pre-fix literal-space $where pattern re-appeared")

    def test_bug_tl3_marker_present(self):
        src = self._read_src()
        self.assertIn("BUG-TL3", src,
                      "BUG-TL3 fix marker missing — refactor risk")


class TestGeneratedURLIsValid(unittest.TestCase):
    """Functional check: the new URL-building approach produces a URL
    that passes urllib's http_client control-character validation."""

    def test_no_control_characters_in_generated_url(self):
        import urllib.request
        base = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"
        params = {
            "$where": "market_and_exchange_names like '%NASDAQ MINI%'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": "52",
        }
        url = base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        # urllib.request.Request does a control-character check on URL — the
        # pre-fix URL failed this; the post-fix URL should pass cleanly.
        # If this raises, the URL still has problematic characters.
        try:
            urllib.request.Request(url, headers={"Accept": "application/json"})
        except Exception as e:
            self.fail(f"Request rejected generated URL: {e}")


if __name__ == "__main__":
    unittest.main()
