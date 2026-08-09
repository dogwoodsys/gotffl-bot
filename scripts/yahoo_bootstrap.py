#!/usr/bin/env python3
"""One-time Yahoo OAuth bootstrap.

Yahoo's refresh token can only be obtained through an interactive browser
consent, which a Lambda cannot perform. Run this once on a laptop, signed in as
the league member whose account authorizes the app. Everything after this is
automatic — the poller refreshes and rotates the token itself.

    AWS_PROFILE=gotffl-admin python scripts/yahoo_bootstrap.py

Nothing is written to disk. The tokens go straight to Parameter Store as
SecureString.
"""

import argparse
import getpass
import sys
import urllib.parse

import boto3
import requests

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
REDIRECT_URI = "oob"  # Yahoo shows the code on screen; no callback server needed
PREFIX = "/gotffl/yahoo"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="ca-central-1")
    parser.add_argument("--prefix", default=PREFIX)
    args = parser.parse_args()

    client_id = input("Yahoo Client ID (Consumer Key): ").strip()
    client_secret = getpass.getpass("Yahoo Client Secret (hidden): ").strip()
    if not client_id or not client_secret:
        print("Both values are required.", file=sys.stderr)
        return 1

    query = urllib.parse.urlencode(
        {"client_id": client_id, "redirect_uri": REDIRECT_URI, "response_type": "code"}
    )
    print("\n1. Open this URL and approve access:\n")
    print(f"   {AUTH_URL}?{query}\n")
    code = input("2. Paste the code Yahoo displays: ").strip()

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=(5, 30),
    )
    if not response.ok:
        print(f"\nYahoo rejected the exchange ({response.status_code}): {response.text}",
              file=sys.stderr)
        return 1

    payload = response.json()
    ssm = boto3.client("ssm", region_name=args.region)
    for name, value in [
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("refresh_token", payload["refresh_token"]),
    ]:
        ssm.put_parameter(
            Name=f"{args.prefix}/{name}", Value=value, Type="SecureString", Overwrite=True
        )
        print(f"   wrote {args.prefix}/{name}")

    # The access token is deliberately not written: the poller fetches a fresh
    # one on first run, so there is no stale hour-old token to reason about.
    print("\nDone. The poller will mint its own access token on first run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
