#!/usr/bin/env python3
"""CloudWatch A2A Runtime — uses Gateway MCP for cross-account AWS access.

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
import logging
import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from gateway_client import ResilientMCPClientManager
from context_tools import create_context_agent, create_a2a_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLOUDWATCH_PROMPT = """You are an expert AWS CloudWatch assistant. When providing information:
1. Provide actionable information with specific values and timestamps
2. For alarms: include alarm name, state, threshold, metric being monitored, and when triggered
3. Use severity labels: [CRITICAL] for critical alarms, [WARNING] for warnings, [OK] for healthy states
4. Format with markdown: **bold** for important info, `code` for resource names
5. Always suggest 2-3 relevant follow-up questions or actions
6. Explain technical data in business terms
7. Include time context and proactively mention related resources
8. IMPORTANT: When alarms are found, clearly state alarm names and severity for workflow processing

**CONCISENESS RULES (mandatory — reduces token usage and prevents timeouts):**
- List max 20 items per category. If more exist, say "Showing 20 of N total. Ask to see more."
- Log groups: show name only (no ARN, no creation date). Group by common prefix if >10 groups.
- Alarms: one line each — wrap alarm name in backticks, then state and metric. Example: `my-alarm` — ALARM — CPUUtilization > 80%
- Metrics: show name + namespace + last value only (no full JSON)
- ALWAYS wrap alarm names in backticks so they are extractable (e.g., `alarm-name-here`)
- ALWAYS include the phrase "N active alarms" when summarizing alarm counts (e.g., "2 active alarms")
- Never dump raw JSON API responses. Always format as readable summary.
- Keep total response under 500 words unless explicitly asked for more detail.
"""


# Lazy initialization — defer Gateway connection to first request to stay within 30s init limit.
# The A2A runtime boots instantly (FastAPI + ping ready in <3s).
# Gateway connection + tool listing happens on first actual invocation.
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
            name="CloudWatch Monitor",
            description="Monitors AWS CloudWatch alarms, metrics, and logs across customer accounts",
            system_prompt=CLOUDWATCH_PROMPT,
            mcp_client=mcp_client,
        )
        _a2a_server = create_a2a_server(agent, _runtime_url)
        logger.info("CloudWatch A2A server initialized")
    return _a2a_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _client_mgr:
        _client_mgr.close()


def ping():
    return {"status": "healthy", "agent": "cloudwatch"}


def create_app() -> FastAPI:
    app = FastAPI(title="CloudWatch A2A Runtime", lifespan=lifespan)
    app.add_api_route("/ping", ping, methods=["GET"])
    # Mount lazily — Gateway connection happens on first actual request
    app.mount("/", _LazyA2AApp())
    logger.info("CloudWatch A2A Runtime ready (Gateway connection deferred to first request)")
    return app


class _LazyA2AApp:
    """ASGI wrapper that lazily initializes the A2A server on first request."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # Handle lifespan events without initializing
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        server = _get_a2a_server()
        await server.to_fastapi_app()(scope, receive, send)


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing
