"""
Handles Zoho OAuth access tokens.

Zoho access tokens expire every ~1 hour. We use the long-lived refresh_token
(generated once via generate_refresh_token.py) to mint a fresh access token
whenever needed, and cache it in memory until it's close to expiring.test
"""
import time
import requests
import config

_cached_token = {"value": None, "expires_at": 0, "scope": ""}


class ZohoAuthError(Exception):
    pass


def get_access_token() -> str:
    """Return a valid Zoho access token, refreshing it if needed."""
    now = time.time()
    if _cached_token["value"] and now < _cached_token["expires_at"] - 60:
        return _cached_token["value"]

    if not (config.ZOHO_CLIENT_ID and config.ZOHO_CLIENT_SECRET and config.ZOHO_REFRESH_TOKEN):
        raise ZohoAuthError(
            "Zoho credentials missing. Check ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / "
            "ZOHO_REFRESH_TOKEN in your .env file (see README for how to generate them)."
        )

    try:
        resp = requests.post(
            config.TOKEN_URL,
            params={
                "refresh_token": config.ZOHO_REFRESH_TOKEN,
                "client_id": config.ZOHO_CLIENT_ID,
                "client_secret": config.ZOHO_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise ZohoAuthError(f"Could not reach Zoho accounts server ({config.TOKEN_URL}): {e}")

    try:
        data = resp.json()
    except ValueError:
        raise ZohoAuthError(
            f"Zoho accounts server returned a non-JSON response (HTTP {resp.status_code}). "
            "Check ZOHO_DOMAIN in your .env matches your account's region."
        )

    if "access_token" not in data:
        raise ZohoAuthError(f"Failed to refresh Zoho access token: {data}")

    _cached_token["value"] = data["access_token"]
    _cached_token["expires_at"] = now + int(data.get("expires_in", 3600))
    _cached_token["scope"] = data.get("scope", "")
    return _cached_token["value"]


def auth_header() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}


def granted_scopes() -> set[str]:
    """Scopes on the current refresh token, as reported by Zoho on refresh."""
    get_access_token()
    return {x.strip() for x in _cached_token["scope"].replace(",", " ").split() if x.strip()}
