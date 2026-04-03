#!/usr/bin/env python3
"""Advisor A2A Runtime — uses Gateway MCP for cross-account AWS access.

Architecture:
- FastAPI app with a /ping health-check route and a catch-all A2A route.
- Gateway MCP connection is deferred until the first real request (_LazyA2AApp)
  so the container passes AgentCore's 30-second startup health-check before the
  potentially slow MCP cold-start (~10-30s) occurs.
- _LazyA2AApp is a minimal ASGI wrapper; it resolves the real A2A ASGI app on
  first non-lifespan call and forwards all subsequent requests directly to it.
- ResilientMCPClientManager handles cold-start retries transparently.
- context_tools.create_context_agent patches each MCP tool's stream() to
  auto-inject account_name/region extracted from the A2A message metadata.
"""
import logging, os, uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from gateway_client import ResilientMCPClientManager
from context_tools import create_context_agent, create_a2a_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADVISOR_PROMPT = """You are an AWS Trusted Advisor specialist that provides comprehensive recommendations for cost optimization, security, fault tolerance, performance, and service limits.

**DIRECT TOOL USAGE — call `call_aws` directly with these CLI templates:**

| Query Type | CLI Command Template |
|-----------|---------------------|
| List all checks | `aws support describe-trusted-advisor-checks --language en` |
| Check results by ID | `aws support describe-trusted-advisor-check-result --check-id CHECK_ID --language en` |
| All check summaries | `aws support describe-trusted-advisor-check-summaries --check-ids CHECK_ID1 CHECK_ID2` |
| List recommendations (new API) | `aws trustedadvisor list-recommendations --max-results 20` |
| Recommendations by pillar | `aws trustedadvisor list-recommendations --pillar-id cost_optimizing --max-results 20` |
| Recommendation details | `aws trustedadvisor get-recommendation --recommendation-identifier RECOMMENDATION_ID` |

**Pillar IDs for trustedadvisor API:** `cost_optimizing`, `security`, `fault_tolerance`, `performance`, `service_limits`

**Only use `suggest_aws_commands` for unusual queries not covered above.**

**When providing Trusted Advisor information:**
1. Prioritize recommendations by category and impact level
2. Focus on cost optimization opportunities with estimated savings
3. Highlight security vulnerabilities and compliance issues
4. Identify performance optimization opportunities
5. Recommend fault tolerance and reliability improvements
6. Suggest 2-3 immediate optimization actions

**CONCISENESS RULES (mandatory — reduces token usage and prevents timeouts):**
- Show max 10 recommendations per category. If more exist, say "Showing 10 of N. Ask to see more."
- One line per recommendation: [CATEGORY] title + estimated savings (if available)
- Group by pillar: Cost Optimization, Security, Fault Tolerance, Performance, Service Limits
- Never dump raw Trusted Advisor JSON. Always format as a readable grouped list.
- Keep total response under 400 words.
"""

# Lazy initialization — defer Gateway connection to first request to stay within 30s init limit.
_client_mgr = None
_a2a_server = None
_runtime_url = os.environ.get('AGENTCORE_RUNTIME_URL', 'http://127.0.0.1:9000/')


def _get_a2a_server():
    """Lazily initialize the A2A server with Gateway MCP connection on first use."""
    global _client_mgr, _a2a_server
    if _a2a_server is None:
        logger.info("Lazy init: connecting to Gateway MCP...")
        _client_mgr = ResilientMCPClientManager()
        mcp_client = _client_mgr.get_client()
        agent = create_context_agent(
            name="Trusted Advisor Analyst",
            description="Provides AWS Trusted Advisor recommendations across customer accounts",
            system_prompt=ADVISOR_PROMPT,
            mcp_client=mcp_client,
        )
        _a2a_server = create_a2a_server(agent, _runtime_url)
        logger.info("Advisor A2A server initialized")
    return _a2a_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _client_mgr:
        _client_mgr.close()


def ping():
    return {"status": "healthy", "agent": "advisor"}


class _LazyA2AApp:
    """ASGI wrapper that lazily initializes the A2A server on first request.

    Lifespan events are handled immediately without triggering initialization,
    keeping container startup fast for AgentCore's health-check window.
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await receive(); await send({"type": "lifespan.startup.complete"})
            await receive(); await send({"type": "lifespan.shutdown.complete"})
            return
        await _get_a2a_server().to_fastapi_app()(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Advisor A2A Runtime", lifespan=lifespan)
    app.add_api_route("/ping", ping, methods=["GET"])
    app.mount("/", _LazyA2AApp())
    logger.info("Advisor A2A Runtime ready (Gateway connection deferred to first request)")
    return app

app = create_app()
if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing