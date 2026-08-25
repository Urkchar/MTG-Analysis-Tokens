from typing import Optional
import requests
import re
import xml.etree.ElementTree as ET
import logging

from scryfall import Scryfall
from EDHRec import EDHRec

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")


def _extract_card_name(card: dict, name_key: str) -> Optional[str]:
    """Extract card name from card dict, handling both simple and card
    faces."""
    if name_key in card:
        return card[name_key]
    if "card_faces" in card and card["card_faces"]:
        return card["card_faces"][0].get(name_key)
    return None


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
    for card in cards:
        if flavor_name := _extract_card_name(card, "flavor_name"):
            invalid_commanders.add(flavor_name)

    # Some commanders have in-universe versions, but not flavor names
    for card in scryfall_client.search(q_in="slx", q_is="commander"):
        invalid_commanders.add(card["name"])

    # Themberchaud has an in-universe version, but its name is the same.
    invalid_commanders.discard("Themberchaud")

    # Commanders from Through the Omenpaths have no flavor name, but are
    # the same mechanically as their orginal version from Marvel's
    # Spider-Man
    for card in scryfall_client.search(q_set="om1", q_is="commander"):
        if printed_name := _extract_card_name(card, "printed_name"):
            invalid_commanders.add(printed_name)

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

    client = EDHRec(rate_per_sec=1)

    for commander in sorted(commanders)[:5]:
        client.save_decks(commander)


if __name__ == "__main__":
    main()
