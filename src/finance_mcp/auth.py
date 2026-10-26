"""OAuth for claude.ai and Hermes: "Sign in with GitHub", restricted to an allowlist of GitHub usernames.

claude.ai and Hermes talk OAuth to this server; FastMCP's OAuthProxy forwards the login to
GitHub. Anyone can *start* the login, but every MCP request re-checks the GitHub
user behind the token, and only usernames in ALLOWED_GITHUB_USERS get through.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

from .settings import Settings

log = logging.getLogger(__name__)


class AllowlistGitHubVerifier(GitHubTokenVerifier):
    def __init__(self, allowed_logins: list[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed = {login.lower() for login in allowed_logins}

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        login = str(result.claims.get("login") or "").lower()
        if login not in self.allowed:
            log.warning("Rejected GitHub user %r (not in ALLOWED_GITHUB_USERS)", login)
            return None
        return result


def build_auth(settings: Settings) -> OAuthProxy:
    missing = [
        name
        for name, value in (
            ("GITHUB_CLIENT_ID", settings.github_client_id),
            ("GITHUB_CLIENT_SECRET", settings.github_client_secret),
            ("ALLOWED_GITHUB_USERS", settings.allowed_github_users),
            ("JWT_SIGNING_KEY", settings.jwt_signing_key),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"AUTH_MODE=github needs: {', '.join(missing)}")
    if not settings.public_base_url.startswith("https://"):
        raise SystemExit("PUBLIC_BASE_URL must be https:// when AUTH_MODE=github")

    verifier = AllowlistGitHubVerifier(
        allowed_logins=settings.allowed_github_users,
        required_scopes=["read:user"],  # only the public profile, nothing else
        cache_ttl_seconds=300,
    )
    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=settings.github_client_id,
        upstream_client_secret=settings.github_client_secret,
        token_verifier=verifier,
        base_url=settings.public_base_url,
        issuer_url=settings.public_base_url,
        # Only these callbacks may receive authorization codes: Claude's own, and Hermes'
        # loopback listener (fixed by oauth.redirect_port: 8765 in Hermes' config.yaml).
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://127.0.0.1:8765/callback",
        ],
        jwt_signing_key=settings.jwt_signing_key,
        require_authorization_consent=True,
    )
