import time
import threading
import requests
from email.utils import parsedate_to_datetime
from typing import Optional
import random
import logging


logging.getLogger("urllib3").setLevel(logging.WARNING)


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
    Retries on 403, 408, 425, 429, 5xx and common transient network 
    errors/timeouts.
    """
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in {403, 408, 425, 429, 500, 502, 503, 504}:
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


def _is_rate_limit_redirect(resp: requests.Response) -> bool:
    """
    Check if response contains a rate limit redirect pattern.
    Some servers return 200 with pageProps.__N_REDIRECT == '/429' 
    instead of actual 429 status code.
    """
    if resp.status_code != 200:
        return False

    try:
        data = resp.json()
        page_props = data.get("pageProps", {})
        return page_props.get("__N_REDIRECT") == "/429"
    except (ValueError, KeyError, AttributeError):
        return False


def _compute_backoff_seconds(
    attempt: int,
    base_delay: float,
    max_delay: float
    ) -> float:
    """Exponential backoff with full jitter."""
    backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return random.uniform(0, backoff)


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
        """Block until at least n tokens are available, then consume 
        them."""
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


class HTTPClient:
    def __init__(
        self,
        rate_per_sec: float = 1.0,
        burst: int = 10,
        timeout: float = 10.0,
        max_retries: int = 5,
        base_delay: float = 30.0,
        max_delay: float = 1000.0
    ):
        self._limiter = TokenBucket(rate_per_sec=rate_per_sec, capacity=burst)
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.headers = {
            "User-Agent": "MTGAnalysisTokens/1.0",
            "Accept": "application/json;q=0.9,*/*;q=0.8"
        }

    def get(self, url: str, **request_kwargs) -> Optional[requests.Response]:
        """Resilient GET with rate limiting, retries, and exponential 
        backoff."""
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = requests.get(url, timeout=self.timeout,
                                    headers=self.headers, **request_kwargs)
                if _is_rate_limit_redirect(resp):
                    resp.status_code = 429  # Treat as rate limit error
                    raise requests.HTTPError(
                        "Rate limit redirect", response=resp)
                resp.raise_for_status()
                return resp
            except Exception as e:
                if (not _is_retryable_request_error(e)
                    or attempt >= self.max_retries):
                    # logging.error(f"Failed to fetch {url}: {e}")
                    # logging.error(e)
                    raise

                # Determine sleep duration
                sleep_for = self.base_delay
                if (isinstance(e, requests.HTTPError)
                    and e.response is not None):
                    retry_after = _parse_retry_after_seconds(
                        e.response.headers.get("Retry-After"))
                    if retry_after is not None:
                        sleep_for = min(retry_after, self.max_delay)
                    else:
                        sleep_for = _compute_backoff_seconds(
                            attempt + 1, self.base_delay, self.max_delay)
                else:
                    sleep_for = _compute_backoff_seconds(
                        attempt + 1, self.base_delay, self.max_delay)

                time.sleep(sleep_for)


if __name__ == "__main__":
    client = HTTPClient(rate_per_sec=10.0)
    try:
        url = ("https://api.scryfall.com/cards/search?"
               "q=prints%3E1+is%3Acommander")
        response = client.get(url)
        if response:
            data = response.json()
            print(f"Fetched {len(data.get('data', []))} cards.")
    except Exception as e:
        print(f"Failed to fetch data: {e}")
