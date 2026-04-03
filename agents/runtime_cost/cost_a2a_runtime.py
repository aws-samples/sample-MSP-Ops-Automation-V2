#!/usr/bin/env python3
"""Cost A2A Runtime — uses Gateway MCP for cross-account AWS access.

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

Date enrichment: Unlike other specialist runtimes, cost uses _create_cost_a2a_server
instead of the generic create_a2a_server. It parses natural language time periods
(e.g., "last 4 months") via RobustDateParser and injects explicit start/end dates
into the prompt before the agent runs. This mirrors the pattern in
supervisor_tools.py analyze_costs() and prevents the LLM from guessing dates.
"""
import logging, os, uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from gateway_client import ResilientMCPClientManager
from context_tools import create_context_agent, create_a2a_server
from robust_date_parser import RobustDateParser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COST_PROMPT = """You are an AWS Cost Explorer specialist that provides comprehensive cost analysis and optimization recommendations.

**DIRECT TOOL USAGE — call `call_aws` directly with these CLI templates:**

For common Cost Explorer operations, use `call_aws` immediately with the correct CLI command:

| Query Type | CLI Command Template |
|-----------|---------------------|
| Monthly cost by service | `aws ce get-cost-and-usage --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD --granularity MONTHLY --metrics BlendedCost --group-by Type=DIMENSION,Key=SERVICE` |
| Daily cost trend | `aws ce get-cost-and-usage --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD --granularity DAILY --metrics BlendedCost` |
| Cost forecast | `aws ce get-cost-forecast --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD --metric BLENDED_COST --granularity MONTHLY` |
| Cost by region | `aws ce get-cost-and-usage --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD --granularity MONTHLY --metrics BlendedCost --group-by Type=DIMENSION,Key=REGION` |
| Top services this month | `aws ce get-cost-and-usage --time-period Start=YYYY-MM-01,End=TODAY --granularity MONTHLY --metrics BlendedCost --group-by Type=DIMENSION,Key=SERVICE` |

**Only use `suggest_aws_commands` for unusual or complex queries not covered above.**

**CRITICAL: Using Provided Time Periods**
When the query includes "TIME PERIOD CONTEXT" with start_date and end_date:
- YOU MUST use those EXACT dates in your call_aws CLI command
- DO NOT generate your own dates or use different date ranges
- Format: --time-period Start=YYYY-MM-DD,End=YYYY-MM-DD

**Important Notes:**
- All Cost Explorer API calls must use region us-east-1 (Cost Explorer is global)
- Group by SERVICE for service breakdown, by REGION for regional analysis

**When providing Cost Explorer information:**
1. Focus on cost optimization opportunities and savings potential
2. Present cost data in clear, business-friendly terms with trends
3. Highlight significant cost increases or decreases with explanations
4. Identify top spending services and resources for optimization
5. Suggest specific cost reduction strategies

**CONCISENESS RULES (mandatory — reduces token usage and prevents timeouts):**
- Show TOP 10 services by cost only. Summarize the rest as "Other (N services): $X.XX total"
- Use compact one-line format per service: Service | $Cost | % of Total
- For multi-month queries: show one total-cost line per month first, then TOP 5 services for the latest month only
- Round all costs to 2 decimal places
- Never dump raw Cost Explorer JSON. Always format as a summary table.
- Keep total response under 400 words.
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
            name="Cost Analyst",
            description="Analyzes AWS costs and optimization opportunities across customer accounts",
            system_prompt=COST_PROMPT,
            mcp_client=mcp_client,
        )
        _a2a_server = _create_cost_a2a_server(agent, _runtime_url)
        logger.info("Cost A2A server initialized")
    return _a2a_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _client_mgr:
        _client_mgr.close()


def ping():
    return {"status": "healthy", "agent": "cost"}


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
    app = FastAPI(title="Cost A2A Runtime", lifespan=lifespan)
    app.add_api_route("/ping", ping, methods=["GET"])
    app.mount("/", _LazyA2AApp())
    logger.info("Cost A2A Runtime ready (Gateway connection deferred to first request)")
    return app

# Date enrichment for cost queries — same logic as supervisor_tools.py analyze_costs()
_date_parser = RobustDateParser(llm_agent=None)
_TIME_KEYWORDS = ['cost', 'spending', 'bill', 'month', 'year', 'quarter', 'week', 'day',
                  'recent', 'last', 'past', 'today', 'yesterday', 'spend', 'budget', 'expense']


def _enrich_prompt_with_dates(prompt: str) -> str:
    """Parse time period from natural language and inject explicit dates."""
    if any(keyword in prompt.lower() for keyword in _TIME_KEYWORDS):
        try:
            date_range = _date_parser.parse_time_period(prompt)
            logger.info(f"Date enrichment: {date_range.period_days} days "
                       f"({date_range.start_date} to {date_range.end_date}), "
                       f"confidence={date_range.confidence:.1%}, method={date_range.method_used}")
            
            time_context = f"""

TIME PERIOD CONTEXT - USE THESE EXACT DATES:
Start date: {date_range.start_date}
End date: {date_range.end_date}
Days: {date_range.period_days}
Confidence: {date_range.confidence:.1%}
"""
            return prompt + time_context
        except Exception as e:
            logger.warning(f"Date enrichment failed: {e}, proceeding without time context")
    return prompt


def _create_cost_a2a_server(agent, runtime_url):
    """Create A2A server with date enrichment + metadata extraction."""
    from context_tools import _extract_metadata_prompt
    from strands.multiagent.a2a import A2AServer
    
    original_stream = agent.stream_async

    async def patched_stream(content_blocks, **kwargs):
        if content_blocks:
            block = content_blocks[0]
            # Step 1: Extract metadata (sets account context)
            if hasattr(block, 'text'):
                block.text = _extract_metadata_prompt(block.text)
                # Step 2: Enrich with date context
                block.text = _enrich_prompt_with_dates(block.text)
            elif isinstance(block, dict) and 'text' in block:
                block['text'] = _extract_metadata_prompt(block['text'])
                block['text'] = _enrich_prompt_with_dates(block['text'])
        async for event in original_stream(content_blocks, **kwargs):
            yield event

    agent.stream_async = patched_stream
    return A2AServer(agent=agent, http_url=runtime_url, serve_at_root=True)


app = create_app()
if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing
