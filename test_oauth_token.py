"""
Test a Twitch OAuth token from twitch_tokens.json or direct arguments.

Usage:
    python test_oauth_token.py \
        --app-name APP_NAME \
        --token-name TOKEN_NAME \
        [--config-path CONFIG_PATH]

    Or, provide values directly:
    python test_oauth_token.py \
        --client-id CLIENT_ID \
        --token TOKEN

Notes:
    --app-name and --token-name will load client_id and access_token from the config file.
    --client-id and --token can be provided directly to override config.
    --config-path defaults to twitch_tokens.json in the script directory.
"""

import argparse
import os
import json
import requests

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitch_tokens.json")
HELIX_USERS_URL = "https://api.twitch.tv/helix/users"


def load_config(config_path: str) -> dict:
    if not config_path:
        return {}
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_token_from_config(config, app_name, token_name):
    try:
        app_entry = config["apps"][app_name]
        client_id = app_entry["client_id"]
        token_entry = app_entry["tokens"][token_name]
        access_token = token_entry["access_token"]
        return client_id, access_token
    except Exception:
        raise RuntimeError(
            f"Could not find client_id or access_token for app-name '{app_name}' and token-name '{token_name}'"
        )


def test_token(client_id, token):
    """Quick test to see if the token is valid.

    The function simply sends a GET to the Twitch Helix `/users` endpoint with
    the supplied `Client-ID` and OAuth bearer token. A `200` response means the
    token is accepted and is associated with a real user; a `401` indicates the
    token is invalid or unauthorized.  Common pitfalls:

    * Using the **client secret** instead of an OAuth access token
    * Passing an **app access token** to an endpoint that requires a user token
    * Forgetting to strip a leading ``oauth:`` prefix
    * Mixing up tokens from different applications
    
    The script will already print the response body when the status is not 200, so
    the error message from Twitch is visible.
    """
    print(f"Using Client ID: {client_id}")
    print(f"Using Access Token: {token}\n")
    url = HELIX_USERS_URL
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    print(f"Token validation: {r.status_code}")
    if r.status_code == 200:
        user = r.json()["data"][0]
        print(f"Token valid for user: {user['login']}")
    else:
        print(f"Token invalid: {r.text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Test a Twitch OAuth token from twitch_tokens.json"
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Twitch Client ID (optional, will use config if not provided)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Access token (optional, will use config if not provided)",
    )
    parser.add_argument(
        "--app-name",
        required=False,
        help="App name key in config (required if not providing --client-id and --token)",
    )
    parser.add_argument(
        "--token-name",
        required=False,
        help="Token name key in config (required if not providing --client-id and --token)",
    )
    parser.add_argument(
        "--config-path",
        default=DEFAULT_CONFIG_PATH,
        help="Path to twitch_tokens.json (default: alongside script)",
    )

    args = parser.parse_args()

    client_id = args.client_id
    token = args.token

    if not client_id or not token:
        if not args.app_name or not args.token_name:
            parser.error("Either --client-id and --token, or both --app-name and --token-name must be provided.")
        config = load_config(args.config_path)
        loaded_client_id, loaded_token = get_token_from_config(config, args.app_name, args.token_name)
        if not client_id:
            client_id = loaded_client_id
        if not token:
            token = loaded_token

    test_token(client_id, token)