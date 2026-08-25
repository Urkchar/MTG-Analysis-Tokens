import requests
import time

from requests.adapters import HTTPAdapter, Retry


class HTTPClient:
    def __init__(
        self,
        max_retries: int
    ):
        self.headers = {
            "User-Agent": "MTGAnalysisTokens/1.0",
            "Accept": "application/json;q=0.9,*/*;q=0.8"
        }

        self.session = requests.Session()
        retries = Retry(
            total=max_retries,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504]
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def get(self, url: str, **request_kwargs):
        resp = self.session.get(url, headers=self.headers, **request_kwargs)

        if self._is_429(resp):
            resp.status_code = 429

        resp.raise_for_status()

        return resp

    def _is_429(self, resp: requests.Response) -> bool:
        """
        Return True if resp was redirected to https://edhrec.com/429,
        False otherwise.
        """
        if resp.status_code != 200:
            return False
        
        try:
            data = resp.json()
            page_props = data.get("pageProps", {})
            return page_props.get("__N_REDIRECT") == "/429"
        except (ValueError, KeyError, AttributeError):
            return False
