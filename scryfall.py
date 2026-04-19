import requests

from token_bucket import HTTPClient


class Scryfall:
    def __init__(self, requests_per_second: float = 10.0):
        self.base_url = "https://api.scryfall.com"
        self._http_client = HTTPClient(rate_per_sec=requests_per_second)

    def search(self,
               q_is: str = None,
               q_has: str = None,
               q_set: str = None,
               q_in: str = None,
               q_prints: str = None,
               q_fo: str | list = None
               ) -> list:

        query_parts = []
        if q_has:
            query_parts.append(f"has:{q_has}")
        if q_set:
            query_parts.append(f"set:\"{q_set}\"")
        if q_in:
            query_parts.append(f"in:\"{q_in}\"")
        if q_is:
            query_parts.append(f"is:{q_is}")
        if q_prints:
            query_parts.append(f"prints{q_prints}")
        if q_fo:
            if type(q_fo) is list:
                joined = " OR ".join(f"fo:\"{item}\"" for item in q_fo)
                query_parts.append(f"({joined})")
            elif type(q_fo) is str:
                query_parts.append(f"fo:\"{q_fo}\"")
        query = "+".join(query_parts)

        url = self.base_url + "/cards/search" + f"?q={query}"
        cards = []
        page = 0
        while url:
            page += 1
            try:
                resp = self._http_client.get(url)
            except requests.exceptions.HTTPError as e:
                data = e.response.json()
                message = (
                    "Your query didn’t match any cards. Adjust your search "
                    "terms or refer to the syntax guide at "
                    "https://scryfall.com/docs/reference")
                if "details" in data and data["details"] == message:
                    return []
                raise
            cards.extend(resp.json().get("data", []))
            url = resp.json().get("next_page")
        return cards

    def bulk_data(self, type: str) -> list:
        bulk_data_types = (
            "oracle_cards",
            "unique_artwork",
            "default_cards",
            "all_cards",
            "rulings"
        )
        if type not in bulk_data_types:
            raise ValueError((
                "Invalid bulk data type, must be one of oracle_cards, "
                "unique_artwork, default_cards, all_cards, rulings"))

        resp = self._http_client.get(self.base_url + f"/bulk-data/{type}")
        j = resp.json()
        data = self._http_client.get(j["download_uri"]).json()
        return data


if __name__ == "__main__":
    s = Scryfall()
    cards = s.search(
        q_fo=["becomes a token", "become tokens"],
        q_has="flavor_name")
    print(len(cards))
    cards = s.search(q_fo="battlefield")
    print(len(cards))
