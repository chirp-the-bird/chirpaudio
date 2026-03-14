#!/usr/bin/env python3
"""
Obtain a Twitch OAuth access token via browser authorization code flow.
No third-party websites; runs a tiny local server to capture the redirect.
The app-name, token-name and twitch-account are just organizational keys for the config file and are not used in the token generation itself.

Usage:
    python get_oauth_token.py \\
        [--client-id CLIENT_ID] \\
        [--client-secret CLIENT_SECRET] \\
        --app-name APP_NAME \\
        --token-name TOKEN_NAME \\
        [--twitch-account TWITCH_LOGIN] \\
        [--scopes SCOPES] \\
        [--config-path CONFIG_PATH] \\
        [--redirect-uri REDIRECT_URI]

    To refresh an existing token:
    python get_oauth_token.py \\
        [--client-id CLIENT_ID] \\
        [--client-secret CLIENT_SECRET] \\
        --app-name APP_NAME \\
        --token-name TOKEN_NAME \\
        --refresh \\
        [--config-path CONFIG_PATH]

Notes:
    --client-id and --client-secret are optional if already stored in the config for the given --app-name.
    --app-name and --token-name are required.
    --refresh refreshes the token for the given app-name/token-name.
    If --client-id or --client-secret are not provided as arguments or environment variables, they will be loaded from the config file for the specified app-name.

The obtained access and refresh tokens are written to twitch_tokens.json
(by default in the same directory as this script), organized by app_name
and token_name and associated with the specified Twitch account.
"""

import argparse
import http.server
import os
import threading
import time
import urllib.parse
import webbrowser
from typing import Dict
import json
import datetime

import requests

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitch_tokens.json")

OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"


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


def save_config(config_path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def start_redirect_server(port: int = 8080) -> tuple:
    """Start a tiny HTTP server to capture the OAuth redirect.
    
    Returns (server, code_dict) where code_dict will be populated with
    the authorization code when the redirect arrives.
    """
    code_storage = {}

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # parse the query string from the redirect URL
            query_params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            )
            print(f"[DEBUG] Received request: {self.path}")
            print(f"[DEBUG] Parsed params: {query_params}")
            code_storage.update(query_params)

            # send a friendly response
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h1>Authorization successful!</h1>"
                b"<p>You can close this window and return to the terminal.</p>"
            )

        def log_message(self, fmt, *args):
            # suppress default logging
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, code_storage


def get_oauth_token(
    client_id: str,
    client_secret: str,
    scopes: list = None,
    redirect_uri: str = "http://localhost:8080",
) -> Dict[str, str]:
    """Perform OAuth authorization code flow and return the token response.
    
    Args:
        client_id: Your Twitch application Client ID
        client_secret: Your Twitch application Client Secret
        scopes: List of scope strings (e.g., ['user:read:email', 'bits:read'])
                Defaults to ['user:read:email'] if not provided
        redirect_uri: Where Twitch will redirect after authorization.
                     Must match your app's registered redirect URI.
    
    Returns:
        dict containing 'access_token', 'refresh_token' (if applicable), etc.
    """
    if scopes is None:
        scopes = ["user:read:email"]  # Use a minimal read-only default scope; callers can override this as needed.

    # build the authorization URL
    scope_str = " ".join(scopes)
    auth_url = (
        f"{OAUTH_AUTHORIZE_URL}?client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote(scope_str)}"
    )

    print(f"Opening browser for authorization...")
    print(f"Requested scopes: {', '.join(scopes)}\n")

    # start the local server to catch the redirect
    port = urllib.parse.urlparse(redirect_uri).port or 8080
    server, code_storage = start_redirect_server(port)

    # open the browser
    webbrowser.open(auth_url)

    # wait for the authorization code to arrive
    print("Waiting for authorization (check your browser)...")
    timeout = time.time() + 120  # 2-minute timeout
    while "code" not in code_storage:
        if time.time() > timeout:
            raise TimeoutError("Authorization timeout; user did not authorize within 2 minutes.")
        time.sleep(0.1)

    server.shutdown()

    code = code_storage["code"][0]
    print("[OK] Authorization received.\n")

    # exchange the authorization code for an access token
    print("Exchanging authorization code for access token...")
    token_response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    token_response.raise_for_status()
    return token_response.json()


def refresh_oauth_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> Dict[str, str]:
    """Refresh an OAuth token using the refresh_token."""
    token_response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    token_response.raise_for_status()
    return token_response.json()


def main():
    parser = argparse.ArgumentParser(
        description="Obtain or refresh a Twitch OAuth access token."
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("TWITCH_CLIENT_ID"),
        help="Twitch Client ID (optional if stored in config for app-name)",
    )
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("TWITCH_CLIENT_SECRET"),
        help="Twitch Client Secret (optional if stored in config for app-name)",
    )
    parser.add_argument(
        "--scopes",
        default="user:read:email",
        help="Space-separated list of scopes (default: 'user:read:email')",
    )
    parser.add_argument(
        "--config-path",
        default=DEFAULT_CONFIG_PATH,
        help="Path to twitch_tokens.json (default: alongside script)",
    )
    parser.add_argument(
        "--app-name",
        required=True,
        help="App name key in config (required)",
    )
    parser.add_argument(
        "--token-name",
        required=True,
        help="Token name key in config (required)",
    )
    parser.add_argument(
        "--twitch-account",
        default="",
        help="Twitch account name associated with this token (optional)",
    )
    parser.add_argument(
        "--redirect-uri",
        default="http://localhost:8080",
        help="Redirect URI (must match your app's registered URI)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh an existing token for the given app-name and token-name.",
    )

    args = parser.parse_args()

    # Load config for possible fallback
    config = load_config(args.config_path)
    app_entry = None
    if isinstance(config, dict):
        app_entry = config.get("apps", {}).get(args.app_name, {})

    # Prefer CLI/env, fallback to config
    client_id = args.client_id or (app_entry.get("client_id") if app_entry else None)
    client_secret = args.client_secret or (app_entry.get("client_secret") if app_entry else None)

    if not client_id or not client_secret:
        parser.error(
            "Client ID and Secret are required. "
            "Provide via --client-id/--client-secret, environment variables, or ensure they are stored in the config for the given --app-name."
        )

    if args.refresh:
        # Refresh mode
        try:
            tokens = app_entry["tokens"]
            token_entry = tokens[args.token_name]
            refresh_token_val = token_entry["refresh_token"]
            current_access_token = token_entry.get("access_token", "<none>")
        except Exception:
            raise RuntimeError(
                f"Could not find refresh_token for app-name '{args.app_name}' and token-name '{args.token_name}' in {args.config_path}"
            )

        print(f"Refreshing token for app-name '{args.app_name}', token-name '{args.token_name}'...")
        print(f"Current (expired) access token:\n{current_access_token}\n")
        print(f"Current refresh token being used:\n{refresh_token_val}\n")
        try:
            token_data = refresh_oauth_token(
                client_id,
                client_secret,
                refresh_token_val,
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            now_iso = now.isoformat().replace("+00:00", "Z")
            expires_in = token_data.get("expires_in")
            expires_at = None
            if isinstance(expires_in, int):
                expires_at = (now + datetime.timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")

            token_entry["access_token"] = token_data.get("access_token")
            token_entry["refresh_token"] = token_data.get("refresh_token")
            token_entry["created_at"] = now_iso
            if expires_at:
                token_entry["expires_at"] = expires_at

            save_config(args.config_path, config)

            print("=" * 60)
            print("[OK] REFRESH SUCCESS!")
            print("=" * 60)
            print(f"\nAccess Token:\n{token_data.get('access_token')}\n")
            if "refresh_token" in token_data:
                print(f"Refresh Token:\n{token_data['refresh_token']}\n")
            print(f"Expires in: {token_data.get('expires_in')} seconds")
            print(f"\n[OK] Updated in: {args.config_path}")
            print(f"  app-name: {args.app_name} | token-name: {args.token_name}\n")
        except Exception as e:
            print(f"\n[ERROR] Error refreshing token: {e}")
            raise
        return

    scopes = args.scopes.split()
    print(f"Client ID: {client_id}")
    print(f"Redirect URI: {args.redirect_uri}")
    print(f"Requested scopes: {scopes}\n")

    try:
        token_data = get_oauth_token(
            client_id,
            client_secret,
            scopes=scopes,
            redirect_uri=args.redirect_uri,
        )

        # update config file
        if not isinstance(config, dict):
            config = {}
        apps = config.setdefault("apps", {})
        app_entry = apps.setdefault(args.app_name, {})
        app_entry["client_id"] = client_id
        app_entry["client_secret"] = client_secret
        tokens = app_entry.setdefault("tokens", {})
        token_entry = tokens.setdefault(args.token_name, {})

        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat().replace("+00:00", "Z")
        expires_in = token_data.get("expires_in")
        expires_at = None
        if isinstance(expires_in, int):
            expires_at = (now + datetime.timedelta(seconds=expires_in)).isoformat().replace("+00:00", "Z")

        token_entry["access_token"] = token_data.get("access_token")
        token_entry["token_type"] = "user"
        token_entry["refresh_token"] = token_data.get("refresh_token")
        token_entry["scopes"] = scopes
        if args.twitch_account:
            token_entry["twitch_account"] = args.twitch_account
        else:
            token_entry.setdefault("twitch_account", "")
        token_entry["created_at"] = now_iso
        if expires_at:
            token_entry["expires_at"] = expires_at

        save_config(args.config_path, config)

        access_token = token_data.get("access_token")
        print("=" * 60)
        print("[OK] SUCCESS!")
        print("=" * 60)
        print(f"\nAccess Token:\n{access_token}\n")

        if "refresh_token" in token_data:
            print(f"Refresh Token:\n{token_data['refresh_token']}\n")

        print(f"Expires in: {token_data.get('expires_in')} seconds")
        print(f"\n[OK] Saved to: {args.config_path}")
        print(f"  app-name: {args.app_name} | token-name: {args.token_name}\n")

    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
    except Exception as e:
        print(f"\n[ERROR] Error: {e}")
        raise


if __name__ == "__main__":
    main()
