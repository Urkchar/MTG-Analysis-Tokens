import time
import threading
from typing import Optional
from email.utils import parsedate_to_datetime
import requests
import random
import re
from bs4 import BeautifulSoup
import json
from pathlib import Path
import xml.etree.ElementTree as ET


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        """
        Simple thread-safe token bucket.

        rate_per_sec: tokens added per second (average rate allowed)
        capacity: max burst (bucket size)
        """
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")

        self.rate = float(rate_per_sec)
        self.capacity = int(capacity)
        self.tokens = float(capacity)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now

    def acquire(self, n: float = 1.0):
        """Block until at least n tokens are available, then consume them."""
        if n <= 0:
            return
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                deficit = n - self.tokens
                wait = deficit / self.rate
            # Avoid tight loops; sleep long enough to fill the deficit
            time.sleep(min(wait, 0.25))


# -----------------------------
# Retry/backoff helpers
# -----------------------------
def _parse_retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """
    Parses Retry-After header which may be:
    - Integer seconds (e.g., "3")
    - HTTP-date (e.g., "Wed, 21 Oct 2015 07:28:00 GMT")
    Returns seconds to wait, or None if invalid.
    """
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        dt = parsedate_to_datetime(v)
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except Exception:
        pass
    return None


def _is_retryable_request_error(e: Exception) -> bool:
    """
    Conservative retry policy for requests exceptions.
    Retries on 403, 408, 425, 429, 5xx and common transient network errors/timeouts.
    """
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in {403,408, 425, 429, 500, 502, 503, 504}:
            return True
        return False  # Non-retryable HTTP error

    # Connection/timeout/transient issues
    transient_types = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
        requests.exceptions.SSLError,
    )
    return isinstance(e, transient_types)


def _compute_backoff_seconds(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: str = "full",
) -> float:
    """
    Exponential backoff with optional jitter.
    attempt: 1-based attempt index (1 = first retry)
    jitter: "full" | "equal" | "none" | "decorrelated"
    """
    backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))

    if jitter == "full":
        return random.uniform(0, backoff)
    elif jitter == "equal":
        return backoff / 2.0 + random.uniform(0, backoff / 2.0)
    elif jitter == "decorrelated":
        # Decorrelated jitter: next = rand(base, backoff*3) but cap at max_delay
        return min(max_delay, random.uniform(base_delay, backoff * 3))
    else:  # "none"
        return backoff


class EDHRec:
    def __init__(
            self,
            *,
            session: Optional[requests.Session] = None,
            rate_per_sec: float = 5.0,
            burst: int = 10
        ):
        self.session = session or requests.Session()
        self._limiter = TokenBucket(rate_per_sec=rate_per_sec, capacity=burst)

        self.base_url = "https://edhrec.com/"
        self.base_next_js_url = f"{self.base_url}/_next/data/"
        self.build_id = self.get_build_id()


    def _get(
            self,
            url: str,
            *,
            timeout: float = 10.0,   # per-request timeout (seconds)
            max_retries: int = 5,   # number of retries on transient failures
            base_delay: float = 0.5,   # base backoff (seconds)
            max_delay: float = 8.0,   # max backoff (seconds)
            jitter: str = "full",   # "full" | "equal" | "decorrelated" | "none"
            token_cost: float = 1.0,   # tokens consumed per attempt
            **request_kwargs   # pass extra requests.get kwargs if needed
        ) -> requests.Response:
        """
        Resilient GET with client-side rate limiting, retries, and jittered backoff.
        Honors Retry-After when present. No overall (total) deadline—only per-request timeout.
        """

        attempt = 0

        while True:
            # Rate-limit the attempt (counts even if it fails)
            self._limiter.acquire(token_cost)

            try:
                resp = self.session.get(url, timeout=timeout, **request_kwargs)
                resp.raise_for_status()
                return resp

            except Exception as e:
                # Non-retryable -> propagate
                if not _is_retryable_request_error(e):
                    print(f"Non-retryable error for {url}")
                    raise

                attempt += 1
                if attempt > max_retries:
                    print(f"Exceeded max retries for {url}")
                    raise

                # Prefer server-provided Retry-After when present
                sleep_for = None
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    retry_after = _parse_retry_after_seconds(
                        e.response.headers.get("Retry-After") or e.response.headers.get("retry-after")
                    )
                    if retry_after is not None:
                        sleep_for = min(retry_after, max_delay)

                # Otherwise use exponential backoff + jitter
                if sleep_for is None:
                    sleep_for = _compute_backoff_seconds(
                        attempt, base_delay=base_delay, max_delay=max_delay, jitter=jitter
                    )

                time.sleep(sleep_for)


    def get_build_id(self) -> str:
        """Fetch HTML and parse __NEXT_DATA__ to get buildId."""
        html = self._get(self.base_url).text
        soup = BeautifulSoup(html, "html.parser")
        s = soup.find("script", id="__NEXT_DATA__")
        if not s or not s.string:
            raise RuntimeError("Could not locate __NEXT_DATA__ on the page.")
        
        data = json.loads(s.string)
        build_id = data.get("buildId")

        if not build_id:
            # Try regex fallback
            match = re.search(r'"buildId"\s*:\s*"([^"]+)"', s.string)
            if match:
                build_id = match.group(1)

        if not build_id:
            raise RuntimeError("Could not extract buildId from __NEXT_DATA__")

        return build_id
    

    def build_next_js_url(self, route: str, identifier: str) -> str:
        """identifier is a formatted commander name or a URL hash"""
        url = f"{self.base_next_js_url}/{self.build_id}/{route}/{identifier}.json"
        return url
    

    def get_decks(self, commander: str):
        url = self.build_next_js_url("decks", commander)
        resp = self._get(url)
        data = resp.json()["pageProps"]["data"]["table"]
        return data
    

    def get_deck(self, url_hash: str):
        url = self.build_next_js_url("deckpreview", url_hash)
        resp = self._get(url)
        data = resp.json()["pageProps"]["data"]["panels"]["deckinfo"]["deck_preview"]
        keep = {
            "cedh",
            "coloridentity",
            "commanders",
            "edhrec_tags",
            "savedate",
            "tags",
            "url",
            "urlhash",
            "deck"
        }
        slim = {k: v for k, v in data.items() if k in keep}
        return slim


def save_decks(commander: str, client: EDHRec):
    decks = client.get_decks(commander)
    print(f"Found {len(decks)} decks for commander {commander}")

    Path(f"decks/{commander}").mkdir(parents=True, exist_ok=True)

    for deck in decks:
        url_hash = deck["urlhash"]
        deck_data = client.get_deck(url_hash)
        with open(f"decks/{commander}/{url_hash}.json", "w") as f:
            json.dump(deck_data, f)


def main():
    NS = {"sm": "https://www.sitemaps.org/schemas/sitemap/0.9"}
    DECKS_SITEMAP = "https://edhrec.com/sitemaps/decks.xml"

    # Fetch XML
    resp = requests.get(DECKS_SITEMAP)
    resp.raise_for_status()

    # Parse XML
    root = ET.fromstring(resp.text)
    # print(root)

    commanders = []

    # Find all <url> elements within the sitemap namespace
    for url_tag in root.findall("sm:url", NS):
        # print(f"Processing {url_tag} in sitemap...")
        loc_tag = url_tag.find("sm:loc", NS)
        if loc_tag is None:
            print("Warning: <url> without <loc> found in sitemap; skipping.")
            continue

        url = loc_tag.text.strip()
        # Extract commander name: everything after "/decks/"
        if "/decks/" in url:
            commander = url.split("/decks/", 1)[1].strip("/")
            commanders.append(commander)

    # for commander in commanders[:10]:
    #     print(commander)

    # print(len(commanders))

    client = EDHRec(rate_per_sec=2)

    for commander in commanders[2:10]:  # Limit to first 10 for testing
        save_decks(commander, client)


if __name__ == "__main__":
    main()
