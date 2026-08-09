#!/usr/bin/env python3
"""List every Yahoo fantasy football league on the authorized account.

Run after scripts/yahoo_bootstrap.py. Prints each league's name alongside its
key so you can confirm you have the right one before deploying — a league ID
alone gives no way to tell two leagues apart, and the wrong one would post
another league's trades.

    AWS_PROFILE=gotffl-admin python scripts/find_league_key.py
"""

import argparse
import sys

sys.path.insert(0, "layers/shared/python")

import requests
from shared.yahoo import BASE_URL, _entities, _first
from shared.yahoo_auth import TokenManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="/gotffl/yahoo")
    args = parser.parse_args()

    token = TokenManager(args.prefix).access_token()
    response = requests.get(
        f"{BASE_URL}/users;use_login=1/games;game_keys=nfl/leagues",
        params={"format": "json"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    payload = response.json()

    leagues = []
    for league in _entities(payload, "league"):
        key = _first(league, "league_key")
        name = _first(league, "name")
        teams = _first(league, "num_teams")
        if key:
            leagues.append((str(key), str(name or "?"), teams))

    if not leagues:
        print("No NFL leagues found on this account.", file=sys.stderr)
        print("Check that the authorizing user is actually in the league.", file=sys.stderr)
        return 1

    print(f"\n{len(leagues)} league(s) on this account:\n")
    for key, name, teams in leagues:
        size = f"{teams} teams" if teams else "size unknown"
        print(f"  {name}  ({size})")
        print(f"      deploy with:  -c league_key={key}\n")

    if len(leagues) > 1:
        print("More than one league — confirm which is the right one before deploying.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
