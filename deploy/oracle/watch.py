"""Bounded OCI capacity watch. Public output contains only fixed status labels.

Oracle user sessions, private SSH keys and provider cookies are never inputs.
GitHub OIDC and a restricted OAuth client produce a 20-minute proof-of-possession
session. Neither JWT nor private key is persisted on the runner.
"""
import json
import os
import sys
from urllib.parse import urlencode, urlsplit

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from oci._vendor import requests

REGION = "ap-tokyo-1"
REPOSITORY = "Tahmid447/Open-Video-Downloade"
AUDIENCE = "ovd-oracle-capacity"


class GuardStop(Exception):
    """Expected safety stop; never include private API data in its message."""


def authenticate(*, negative_audience=False):
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise GuardStop("UNTRUSTED_REPOSITORY")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise GuardStop("UNTRUSTED_REF")
    if os.environ.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}:
        raise GuardStop("UNTRUSTED_EVENT")
    domain = os.environ["OCI_DOMAIN_URL"].rstrip("/")
    parsed = urlsplit(domain)
    if (parsed.scheme != "https" or not parsed.hostname or
            not parsed.hostname.endswith(".identity.oraclecloud.com") or
            parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.port):
        raise GuardStop("INVALID_IDENTITY_ENDPOINT")
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    if urlsplit(url).scheme != "https":
        raise GuardStop("INVALID_OIDC_ENDPOINT")
    audience = "ovd-negative-audience-test" if negative_audience else AUDIENCE
    response = requests.get(url + "&" + urlencode({"audience": audience}),
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
        timeout=30, allow_redirects=False)
    if response.status_code != 200:
        raise GuardStop("GITHUB_OIDC_FAILED")
    jwt = response.json()["value"]
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    response = requests.post(domain + "/oauth2/v1/token",
        auth=(os.environ["OCI_CLIENT_ID"], os.environ["OCI_CLIENT_SECRET"]),
        data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
              "requested_token_type": "urn:oci:token-type:oci-rpst",
              "subject_token_type": "jwt", "subject_token": jwt,
              "public_key": public, "res_type": "githubactions", "rpst_exp": "20"},
        timeout=45, allow_redirects=False)
    if negative_audience:
        if response.status_code not in (400, 401, 403):
            raise GuardStop("AUDIENCE_REJECTION_NOT_VERIFIED")
        return None
    if response.status_code != 200:
        # Deliberately do not output the response body, headers or request.
        raise GuardStop("OCI_TOKEN_EXCHANGE_HTTP_" + str(response.status_code))
    token = response.json()["token"]
    return oci.auth.signers.SecurityTokenSigner(token, private)


def main():
    authenticate(negative_audience=True)
    signer = authenticate()
    cfg = {"region": REGION, "tenancy": os.environ["OCI_TENANCY"]}
    iam = oci.identity.IdentityClient(cfg, signer=signer,
        retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(15,45))
    regions = iam.list_region_subscriptions(cfg["tenancy"]).data
    if [r.region_name for r in regions if r.is_home_region] != [REGION]:
        raise GuardStop("HOME_REGION_CHANGED")
    print(json.dumps({"status": "CLOUD_IDENTITY_VERIFIED", "wrong_audience_denied": True,
                      "home_region_verified": True, "resources_created": False}))


if __name__ == "__main__":
    try:
        main()
    except GuardStop as exc:
        print(json.dumps({"status": str(exc)})); sys.exit(1)
    except oci.exceptions.ServiceError as exc:
        print(json.dumps({"status": "OCI_API_ATTENTION", "http_status": exc.status})); sys.exit(1)
    except Exception:
        print(json.dumps({"status": "WATCH_INTERNAL_ERROR"})); sys.exit(1)
