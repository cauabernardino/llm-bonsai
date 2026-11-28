"""The key Prometheus scrapes the gateway with.

/metrics only accepts admin keys. Instead of handing Prometheus the master key, it gets a
key of its own: owned by a read-only admin (`proxy_admin_viewer`) and limited to the
metrics routes, so it can't call models, spend budget or read the management API.
"""

from llm_bonsai import report
from llm_bonsai.clients.gateway import GatewayAdmin
from llm_bonsai.config import EnvFile

SCRAPE_USER = "prometheus"
SCRAPE_KEY_ALIAS = "prometheus-scrape"
SCRAPE_KEY_ENV = "LITELLM_KEY_PROMETHEUS"
# Both spellings: the gateway redirects /metrics to /metrics/.
SCRAPE_LIMITS = {"allowed_routes": ["/metrics", "/metrics/"]}


def ensure_scrape_key(admin: GatewayAdmin, env: EnvFile) -> str:
    """The scrape key from .env if the gateway still knows it, otherwise a new one saved there."""
    key = env.find(SCRAPE_KEY_ENV)
    if key and admin.key_is_valid(key):
        admin.update_key(key, **SCRAPE_LIMITS)
        report.line(
            (SCRAPE_KEY_ENV, "bold"), f": existing key kept, limited to {', '.join(SCRAPE_LIMITS['allowed_routes'])}"
        )
        return key
    admin.ensure_user(SCRAPE_USER, role="proxy_admin_viewer")
    # A key left over from a lost .env would block the alias; remove it first.
    admin.delete_key_alias(SCRAPE_KEY_ALIAS)
    key = admin.generate_user_key(SCRAPE_USER, SCRAPE_KEY_ALIAS, **SCRAPE_LIMITS)
    env.set(SCRAPE_KEY_ENV, key)
    report.line(
        (SCRAPE_KEY_ENV, "bold"), f": new key for read-only admin {SCRAPE_USER!r}, limited to /metrics, saved to .env"
    )
    return key
