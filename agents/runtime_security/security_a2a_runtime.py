#!/usr/bin/env python3
"""Security A2A Runtime — uses Gateway MCP for cross-account AWS access.

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

SECURITY_PROMPT = """You are an AWS Security Hub specialist that provides comprehensive security insights and findings analysis.

**DIRECT TOOL USAGE — call `call_aws` directly with these CLI templates:**

| Query Type | CLI Command Template |
|-----------|---------------------|
| Critical/High findings | `aws securityhub get-findings --filters '{"SeverityLabel":[{"Value":"CRITICAL","Comparison":"EQUALS"},{"Value":"HIGH","Comparison":"EQUALS"}],"RecordState":[{"Value":"ACTIVE","Comparison":"EQUALS"}]}' --max-results 20` |
| All active findings | `aws securityhub get-findings --filters '{"RecordState":[{"Value":"ACTIVE","Comparison":"EQUALS"}]}' --max-results 50` |
| Compliance standards | `aws securityhub get-enabled-standards` |
| Security score/hub status | `aws securityhub describe-hub` |
| Findings by standard | `aws securityhub get-findings --filters '{"ComplianceStatus":[{"Value":"FAILED","Comparison":"EQUALS"}],"RecordState":[{"Value":"ACTIVE","Comparison":"EQUALS"}]}' --max-results 20` |

**Only use `suggest_aws_commands` for unusual queries not covered above.**

**When providing Security Hub information:**
1. Prioritize findings by severity (CRITICAL, HIGH, MEDIUM, LOW)
2. Provide clear summaries with actionable remediation steps
3. Explain compliance status and standards (AWS FSBP, CIS, PCI DSS, NIST)
4. Highlight immediate security risks that need attention
5. Use severity labels: [CRITICAL], [HIGH], [MEDIUM], [LOW] for findings; [COMPLIANT] for passing controls
6. Suggest 2-3 follow-up security actions

**CONCISENESS RULES (mandatory — reduces token usage and prevents timeouts):**
- Show max 15 findings total. If more exist, say "Showing 15 of N. Ask to see more."
- One line per finding: [SEVERITY] resource name + title (no full JSON)
- Group findings by severity (CRITICAL first, then HIGH, MEDIUM, LOW)
- Never dump raw Security Hub JSON. Always format as a readable grouped summary.
- Keep total response under 500 words.
"""

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
            name="Security Analyst",
            description="Analyzes AWS Security Hub findings and compliance across customer accounts",
            system_prompt=SECURITY_PROMPT,
            mcp_client=mcp_client,
        )
        _a2a_server = create_a2a_server(agent, _runtime_url)
        logger.info("Security A2A server initialized")
    return _a2a_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _client_mgr:
        _client_mgr.close()


def ping():
    return {"status": "healthy", "agent": "security"}


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
    app = FastAPI(title="Security A2A Runtime", lifespan=lifespan)
    app.add_api_route("/ping", ping, methods=["GET"])
    app.mount("/", _LazyA2AApp())
    logger.info("Security A2A Runtime ready (Gateway connection deferred to first request)")
    return app

app = create_app()
if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing