"""What the lab consists of, as declared under infra/: namespaces, Services, model groups and teams."""

from llm_bonsai.models import Service, Team

LLM_NAMESPACE = "llm"
OBSERVABILITY_NAMESPACE = "observability"

GATEWAY = Service(LLM_NAMESPACE, "litellm", 4000)
LANGFUSE = Service(OBSERVABILITY_NAMESPACE, "langfuse-web", 3000)
GATEWAY_POD_LABELS = "app.kubernetes.io/name=litellm,app.kubernetes.io/component=proxy"

# Model groups from infra/litellm/values.yaml. chat-broken points at a host that doesn't exist.
CHAT_SMALL = "chat-small"
CHAT_BROKEN = "chat-broken"
EMBED = "embed"

# Budgets are generous on purpose: the probes that need to run out of budget create
# their own throwaway keys, and load/chaos runs spend a few fake dollars each.
PAYMENTS_AI = Team("payments-ai", "LITELLM_KEY_PAYMENTS_AI", [CHAT_SMALL, CHAT_BROKEN], rpm_limit=3, max_budget=1000.0)
SUPPORT_BOT = Team("support-bot", "LITELLM_KEY_SUPPORT_BOT", [CHAT_SMALL, EMBED], rpm_limit=600, max_budget=1000.0)
TEAMS = [PAYMENTS_AI, SUPPORT_BOT]
