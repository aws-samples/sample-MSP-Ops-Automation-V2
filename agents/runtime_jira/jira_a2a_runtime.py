#!/usr/bin/env python3
"""Jira Operations A2A Runtime — uses Gateway MCP for Jira API access.

Follows the same architecture pattern as other specialist runtimes
(cloudwatch, security, cost, advisor, knowledge):
- ResilientMCPClientManager for cold-start retry
- create_context_agent for automatic account_name/region injection
- create_a2a_server for metadata extraction from A2A messages

Architecture:
- FastAPI app with a /ping health-check route and a catch-all A2A route.
- Gateway MCP connection is deferred until the first real request (_LazyA2AApp)
  so the container passes AgentCore's 30-second startup health-check before the
  potentially slow MCP cold-start (~10-30s) occurs.
- _LazyA2AApp is a minimal ASGI wrapper; it resolves the real A2A ASGI app on
  first non-lifespan call and forwards all subsequent requests directly to it.

Refs:
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-a2a.html
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

JIRA_PROMPT = """You are a Jira operations specialist for an MSP operations platform.

**Your capabilities:**

**Issue Management:**
- Create incident tickets with appropriate priority (P1-P4)
- Get detailed information about specific Jira issues
- Update ticket status, assignees, and fields
- Delete issues (use with caution)
- Assign issues to users by accountId
- Search for existing tickets by key, project, or keyword using JQL

**Comments:**
- Get all comments for an issue
- Add comments with incident details (ADF format)

**Workflow:**
- Get available transitions for an issue
- Transition issues through workflow states

**Watchers:**
- Get list of watchers for an issue
- Add watchers to issues
- Remove watchers from issues

**Worklogs:**
- Get all worklogs for an issue
- Add time tracking worklogs with comments

**Issue Links:**
- Create links between issues (blocks, relates to, etc.)
- Delete issue links

**Metadata:**
- List available Jira projects
- Find users by name or email
- Get all issue types
- Get all priorities

**Important guidelines:**
- When creating issues, always include: project key, summary, issue type, and description
- Use the project key from the user's request or default to the configured project
- For searches, construct proper JQL queries
- When getting issue details, use the exact issue key (e.g., MD-100, PROJ-42)
- **CRITICAL: When asked to ADD A COMMENT, ALWAYS use the addComment tool. NEVER use editIssue to update the description.**
- Always use browse URLs in format: https://DOMAIN.atlassian.net/browse/ISSUE-KEY (not REST API URLs)
- Format responses with markdown for readability

**CRITICAL: Adding Comments (Atlassian Document Format)**
When calling the addComment tool, you MUST format the body as ADF (Atlassian Document Format) JSON:
```json
{
  "body": {
    "type": "doc",
    "version": 1,
    "content": [
      {
        "type": "paragraph",
        "content": [
          {
            "type": "text",
            "text": "Your comment text here"
          }
        ]
      }
    ]
  }
}
```

**IMPORTANT: Comment Length Limits**
- Keep comments concise (under 500 words) to avoid token limits
- For long content (detailed guides, multi-step instructions):
  * Summarize the key points in the comment
  * Reference external documentation
  * Or split into multiple shorter comments
- Focus on actionable items, not full documentation

For multi-paragraph comments, add multiple paragraph objects in the content array.

**Response format:**
- For created issues: Include the issue key, URL, and summary
- For searches: List matching issues with key, summary, status, and priority
- For issue details: Show all relevant fields including description, status, assignee, priority
- For added comments: Confirm the comment was added with the issue key
- Always be explicit about what action was taken and the result
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
            name="Jira Operations Manager",
            description="Manages Jira tickets for AWS incidents and operations",
            system_prompt=JIRA_PROMPT,
            mcp_client=mcp_client,
        )
        _a2a_server = create_a2a_server(agent, _runtime_url)
        logger.info("Jira Operations A2A server initialized")
    return _a2a_server


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if _client_mgr:
        _client_mgr.close()


def ping():
    return {"status": "healthy", "agent": "jira"}


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
    app = FastAPI(title="Jira Operations A2A Runtime", lifespan=lifespan)
    app.add_api_route("/ping", ping, methods=["GET"])
    app.mount("/", _LazyA2AApp())
    logger.info("Jira A2A Runtime ready (Gateway connection deferred to first request)")
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing