"""
Run this ONCE to exchange a Zoho "grant token" (generated manually in the
API Console's Generate Code tab) for a permanent refresh token.

Self Client apps don't use a browser redirect - you generate the grant
token yourself in the API Console UI, then this script exchanges it.

Usage:
    python generate_refresh_token.py
"""
import requests
import config

if not (config.ZOHO_CLIENT_ID and config.ZOHO_CLIENT_SECRET):
    print("Set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in your .env file first, then re-run this script.")
    raise SystemExit(1)

print("\nBefore running this, make sure you've generated a grant token in the API Console:")
print("  Self Client -> Generate Code tab -> paste scope:")
print(f"  {config.OAUTH_SCOPES}")
print("  -> set duration (e.g. 10 minutes) -> add a description -> Create\n")
print("The grant token is only valid for the duration you picked, so be quick.\n")

grant_code = input("Paste the grant/authorization token here: ").strip()

resp = requests.post(
    config.TOKEN_URL,
    params={
        "grant_type": "authorization_code",
        "client_id": config.ZOHO_CLIENT_ID,
        "client_secret": config.ZOHO_CLIENT_SECRET,
        "code": grant_code,
    },
    timeout=30,
)
data = resp.json()

if "refresh_token" not in data:
    print("\nSomething went wrong:")
    print(data)
    print("\nCommon causes: grant token expired (they're short-lived, generate a fresh one),")
    print("grant token already used once before, or ZOHO_DOMAIN in .env doesn't match the")
    print("region you generated it in (check the accounts-server value if you have it).")
    raise SystemExit(1)

print("\nSuccess! Add this line to your .env file:\n")
print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}")
print(f"\n(api_domain returned by Zoho: {data.get('api_domain')} - just for your reference)")
