from typing import Optional
import requests
import re
from bs4 import BeautifulSoup
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import logging
import time

from scryfall import Scryfall
from token_bucket import HTTPClient
from utils import format_time

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")


class EDHRec:
    def __init__(self, *, rate_per_sec: float = 1.0):
        self._http_client = HTTPClient(rate_per_sec=rate_per_sec)
        self.base_url = "https://edhrec.com/"
        self.base_next_js_url = f"{self.base_url}/_next/data/"
        self.build_id = self.get_build_id()

    def _get(self, url: str) -> Optional[requests.Response]:
        # TODO: Remove?
        try:
            resp = self._http_client.get(url)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logging.warning(f"URL {url} not found (404).")
                return None
            raise

        return resp

    def get_build_id(self) -> str:
        """Fetch HTML and parse __NEXT_DATA__ to get buildId."""
        html = self._http_client.get(self.base_url).text
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            raise RuntimeError("Could not locate __NEXT_DATA__ on the page.")

        try:
            build_id = json.loads(script.string).get("buildId")
        except json.JSONDecodeError:
            build_id = None

        if not build_id:
            # Try regex fallback
            match = re.search(r'"buildId"\s*:\s*"([^"]+)"', script.string)
            build_id = match.group(1) if match else None

        if not build_id:
            raise RuntimeError("Could not extract buildId from __NEXT_DATA__")

        return build_id

    def set_build_id(self):
        self.build_id = self.get_build_id()

    def build_next_js_url(self, route: str, identifier: str) -> str:
        """identifier is a formatted commander name or a URL hash"""
        return (f"{self.base_next_js_url}{self.build_id}/{route}/"
                f"{identifier}.json")

    def get_decks(self, commander: str):
        url = self.build_next_js_url("decks", commander)
        resp = self._get(url)
        page_props = resp.json()["pageProps"]

        # TODO: resp can be None
        data = page_props["data"]["table"]
        return data

    def get_deck(self, url_hash: str):
        url = self.build_next_js_url("deckpreview", url_hash)
        resp = self._get(url)
        # TODO: resp can be None
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
        decks = self.get_decks(commander)
        logging.info(f"Saving {len(decks)} deck(s) for {commander}...")

        deck_dir = Path(f"decks/{commander}")
        deck_dir.mkdir(parents=True, exist_ok=True)

        new_decks = 0
        for deck in decks:
            deck_file = deck_dir / f"{deck['urlhash']}.json"
            if deck_file.is_file():
                continue  # Skip if we already have this deck

            deck_data = self.get_deck(deck["urlhash"])
            if deck_data:
                deck_file.write_text(json.dumps(deck_data))
                new_decks += 1

        elapsed = time.perf_counter() - start_time
        rate = new_decks / elapsed
        rate_str = f" ({round(rate, 1)}/s)." if new_decks > 1 else "."
        logging.info((f"Saved {new_decks} new deck(s) in "
                      f"{format_time(int(elapsed))}{rate_str}"))

    def count_decks(self, commander: str) -> int:
        # All decks for all commanders: 9,283,036.
        # Not including flavor names: 8,314,401
        return len(self.get_decks(commander))


def format_commander_name(name: str) -> str:
    # EDHRec formats commander names in URLs by lowercasing and
    # replacing non-alphanumerics with hyphens
    formatted = name.lower()
    formatted = formatted.replace("'", "")
    formatted = re.sub(r"[^a-z0-9]+", "-", formatted)
    formatted = re.sub(r"-+", "-", formatted)
    return formatted.strip("-")


def is_valid_commander(commander: str, invalid_commanders: list) -> bool:
    formatted_commander = format_commander_name(commander)

    for invalid_commander in invalid_commanders:
        if invalid_commander in formatted_commander:
            return False
    return True


def get_invalid_commanders() -> set:
    invalid_commanders = set()
    scryfall_client = Scryfall()

    # Some commanders have a flavor name, but are the same card
    # ("Mina Harker" == "Thalia, Guardian of Thraben")
    cards = scryfall_client.search(
        q_has="flavor_name",
        q_prints=">1",
        q_is="commander")

    flavor_names = []
    for card in cards:
        if "flavor_name" in card:
            flavor_names.append(card["flavor_name"])
        else:
            flavor_names.append(card["card_faces"][0]["flavor_name"])

    invalid_commanders.update(flavor_names)

    # Some commanders have in-universe versions, but not flavor names
    cards = scryfall_client.search(q_in="slx", q_is="commander")
    assert len(cards) == 28
    names = [card["name"] for card in cards]
    invalid_commanders.update(names)
    # Themberchaud has an in-universe version, but its name is the same.
    invalid_commanders.remove("Themberchaud")

    # Commanders from Through the Omenpaths have no flavor name, but are
    # the same mechanically as their orginal version from Marvel's
    # Spider-Man
    cards = scryfall_client.search(q_set="om1", q_is="commander")

    names = []
    for card in cards:
        if "printed_name" in card:
            names.append(card["printed_name"])
        else:
            names.append(card["card_faces"][0]["printed_name"])

    invalid_commanders.update(names)

    return {format_commander_name(name) for name in invalid_commanders}


def get_commanders() -> list:
    NS = {"sm": "https://www.sitemaps.org/schemas/sitemap/0.9"}
    resp = requests.get("https://edhrec.com/sitemaps/decks.xml")
    resp.raise_for_status()

    # Parse XML
    root = ET.fromstring(resp.text)

    invalid_commanders = get_invalid_commanders()
    commanders = []

    # Find all <url> elements within the sitemap namespace
    for url_tag in root.findall("sm:url", NS):
        url = url_tag.findtext("sm:loc", "", NS)
        if not url:
            logging.warning("<url> without <loc> found in sitemap; skipping.")
            continue

        # Extract commander name: everything after "/decks/"
        if "/decks/" in url:
            commander = url.split("/decks/", 1)[1].strip("/")
            if is_valid_commander(commander, invalid_commanders):
                commanders.append(commander)

    return commanders


def main():
    commanders = get_commanders()
    logging.info(f"Found {len(commanders)} unique commanders on EDHRec.")

    client = EDHRec(rate_per_sec=2)

    for commander in sorted(commanders):
        client.save_decks(commander)


if __name__ == "__main__":
    main()
