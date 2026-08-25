from bs4 import BeautifulSoup
import json
import logging
import requests
import time
from pathlib import Path

from HTTPClient import HTTPClient
from utils import format_time


class EDHRec:
    def __init__(self, *, rate_per_sec: float = 1.0, max_retries: int = 5):
        if rate_per_sec > 1:
            logging.warning(
                ("rate_per_sec > 1 may result in \"429 Too many requests\" "
                "errors."))
        self.rate = rate_per_sec

        self._http_client = HTTPClient(
            max_retries=max_retries
        )
        self.base_url = "https://edhrec.com/"
        self.base_next_js_url = f"{self.base_url}/_next/data/"

        self.build_id = None
        self._set_build_id()

    def _get_build_id(self) -> str:
        html = self._http_client.get(self.base_url).text
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")

        return json.loads(script.string).get("buildId")

    def _set_build_id(self):
        """Fetch HTML and parse __NEXT_DATA__ to get buildId."""
        previous_build_id = self.build_id
        new_build_id = self._get_build_id()

        while new_build_id == previous_build_id:
            time.sleep(1 / self.rate)
            new_build_id = self._get_build_id()
        
        self.build_id = new_build_id

    def _build_next_js_url(self, route: str, identifier: str) -> str:
        """
        route is either "decks" or "deckpreview".
        identifier is a formatted commander name or a URL hash.
        """
        return (f"{self.base_next_js_url}{self.build_id}/{route}/"
                f"{identifier}.json")

    def get_decks(self, commander: str) -> list:
        """
        Returns a list of dicts with the following keys:
        [
            {
                "artifact": int,
                "battle": int,
                "creature": int,
                "enchantment": int,
                "instant": int,
                "land": int,
                "planeswalker": int,
                "price": int,
                "salt": float,
                "save_date": str,
                "sorcery": int,
                "tags": list[str],
                "urlhash": str
            },
        ]
        """
        url = self._build_next_js_url("decks", commander)
        resp = self._http_client.get(url)
        data = resp.json()["pageProps"]["data"]["table"]

        return data

    def get_deck(self, url_hash: str) -> dict | None:
        url = self._build_next_js_url("deckpreview", url_hash)

        try:
            resp = self._http_client.get(url)
        except requests.exceptions.HTTPError as e:
            # 404 may result from stale build_id
            if e.response is not None and e.response.status_code == 404:
                logging.error(e)
                # Refresh build_id and move on
                self._set_build_id()
                return None
            else:
                raise e

        # TODO: cannot access local variable 'resp' where it is not associated with a value
        data = resp.json()["pageProps"]["data"]
        deck_preview = data["panels"]["deckinfo"]["deck_preview"]
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

        return {k: v for k, v in deck_preview.items() if k in keep}

    def save_decks(self, commander: str):
        start_time = time.perf_counter()

        # Get all the decks for the commander.
        decks = self.get_decks(commander)
        logging.info(f"Saving {len(decks)} deck(s) for {commander}...")

        # Find this commander's decks file or create it if it doesn't exist.
        decks_file = Path(f"decks/{commander}.json")
        existing_decks = []
        if not decks_file.is_file():
            decks_file.touch()
            decks_file.write_text("[]")
        else:
            with open(decks_file, "r", encoding="utf-8") as f:
                existing_decks = json.load(f)

        # Set for checking if we aleady have a deck
        existing_hashes = {d["urlhash"] for d in existing_decks}

        new_decks = 0   # Counter for how many new decks we save
        for deck in decks:
            if deck["urlhash"] in existing_hashes:
                continue  # Skip if we already have this deck

            # Get the detailed data for this deck
            deck_data = self.get_deck(deck["urlhash"])

            if deck_data:
                existing_decks.append(deck_data)
                with open(decks_file, "w", encoding="utf-8") as f:
                    json.dump(existing_decks, f, indent=2, ensure_ascii=False)
                new_decks += 1
                existing_hashes.add(deck["urlhash"])

            time.sleep(1 / self.rate)

        elapsed = time.perf_counter() - start_time
        rate = new_decks / elapsed
        rate_str = f" ({round(rate, 1)}/s)." if new_decks > 1 else "."
        logging.info(
            (f"Saved {new_decks} new deck(s) in "
             f"{format_time(int(elapsed))}{rate_str}"))
    

if __name__ == "__main__":
    client = EDHRec(rate_per_sec=1)

    decks = client.get_decks("aang-airbending-master")
    from pprint import pprint
    pprint(decks)