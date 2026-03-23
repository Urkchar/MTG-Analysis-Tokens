import requests
import re
from bs4 import BeautifulSoup
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from tqdm import tqdm
import logging

from scryfall import Scryfall
from token_bucket import HTTPClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class EDHRec:
    def __init__(
            self,
            *,
            rate_per_sec: float = 1.0,
        ):
        self._http_client = HTTPClient(rate_per_sec=rate_per_sec)
        self.base_url = "https://edhrec.com/"
        self.base_next_js_url = f"{self.base_url}/_next/data/"
        self.build_id = self.get_build_id()

    def get_build_id(self) -> str:
        """Fetch HTML and parse __NEXT_DATA__ to get buildId."""
        html = self._http_client.get(self.base_url).text
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

    def set_build_id(self):
        self.build_id = self.get_build_id()
    
    def build_next_js_url(self, route: str, identifier: str) -> str:
        """identifier is a formatted commander name or a URL hash"""
        url = (f"{self.base_next_js_url}/{self.build_id}/{route}/{identifier}"
                ".json")
        return url
    
    def _get_retry_once(self, route: str, identifier: str):
        url = self.build_next_js_url(route, identifier)

        # build_id could be stale. If we get a 404, refresh the build_id
        # and try again once.
        try:
            resp = self._http_client.get(url)
            return resp
        except requests.exceptions.HTTPError as e:
            logging.warning("HTTPError in _get_retry_once.")
            if e.response is not None and e.response.status_code == 404:
                logging.debug("Status code was 404. Retrying once...")
                self.set_build_id()
                # Rebuild the URL with the new build_id
                url = self.build_next_js_url(route, identifier)
                try:
                    resp = self._http_client.get(url)
                    return resp
                except requests.exceptions.HTTPError as e2:
                    if (e2.response is not None
                        and e2.response.status_code == 404):
                        logging.warning(
                            "Second retry returned 404, returning None.")
                        return None
                    raise
            else:
                logging.error("Status code was not 404. Raising.")
                raise
    
    def get_decks(self, commander: str):
        resp = self._get_retry_once("decks", commander)
        data = resp.json()["pageProps"]["data"]["table"]
        return data
    
    def get_deck(self, url_hash: str):
        resp = self._get_retry_once("deckpreview", url_hash)
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
        slim = {k: v for k, v in deck_preview.items() if k in keep}
        return slim

    def save_decks(self, commander: str):
        decks = self.get_decks(commander)
        print(f"Found {len(decks)} decks for {commander}")

        Path(f"decks/{commander}").mkdir(parents=True, exist_ok=True)

        for deck in tqdm(decks):
            url_hash = deck["urlhash"]
            if Path(f"decks/{commander}/{url_hash}.json").is_file():
                continue  # Skip if we already have this deck
            deck_data = self.get_deck(url_hash)
            if deck_data is not None:
                with open(f"decks/{commander}/{url_hash}.json", "w") as f:
                    json.dump(deck_data, f)

    def count_decks(self, commander: str) -> int:
        decks = self.get_decks(commander)
        return len(decks)   # All decks for all commanders: 9,283,036.
                            # Not including flavor names: 8,314,401


def format_commander_name(name: str) -> str:
    # EDHRec formats commander names in URLs by lowercasing and replacing
    # non-alphanumerics with hyphens
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

    # Commanders from Through the Omenpaths have no flavor name, but are the
    # same mechanically as their orginal version from Marvel's Spider-Man
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
    DECKS_SITEMAP = "https://edhrec.com/sitemaps/decks.xml"

    # Fetch XML
    resp = requests.get(DECKS_SITEMAP)
    resp.raise_for_status()

    # Parse XML
    root = ET.fromstring(resp.text)

    invalid_commanders = get_invalid_commanders()
    commanders = []
    # Find all <url> elements within the sitemap namespace
    for url_tag in root.findall("sm:url", NS):
        loc_tag = url_tag.find("sm:loc", NS)
        if loc_tag is None:
            print("Warning: <url> without <loc> found in sitemap; skipping.")
            continue

        url = loc_tag.text.strip()
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

    for commander in commanders:
        client.save_decks(commander)


if __name__ == "__main__":
    main()
