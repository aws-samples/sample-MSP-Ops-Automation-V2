"""
AWS API MCP Server - Thin Wrapper for Official AWS Labs Package
================================================================
Wraps awslabs.aws-api-mcp-server==1.3.9 with cross-account credential injection.

Official package provides 2 tools: call_aws, suggest_aws_commands
Tools are already registered on their 'server' FastMCP instance.

fastmcp version pinned to 2.14.5:
  awslabs.aws-api-mcp-server uses `from fastmcp import FastMCP` (PyPI fastmcp package),
  NOT `from mcp.server.fastmcp import FastMCP` (mcp stdlib). These are DIFFERENT classes.
  fastmcp 2.14.5 FastMCP API:
    - handler method: mcp._list_tools_mcp  (NOT mcp.list_tools)
    - tool storage: mcp._tool_manager.get_tools() async  (NOT mcp._tool_manager._tools)
    - call_tool: mcp._tool_manager.call_tool(key, args)  (patchable)
    - handler registration: mcp._mcp_server.list_tools()(fn) decorator pattern

Root cause of 421 Misdirected Request (fixed):
  awslabs.aws-api-mcp-server>=1.3.9 has HTTPHeaderValidationMiddleware that checks
  incoming Host header against ALLOWED_HOSTS (defaults to 127.0.0.1).
  AgentCore Gateway sends requests with its internal host header which fails the check.

Fix: Set AWS_API_MCP_ALLOWED_HOSTS=* and AWS_API_MCP_HOST=0.0.0.0 BEFORE importing
  the server module (config.py reads them at module import time via os.getenv).

Schema sanitization for AgentCore Gateway compatibility:
  mcp>=1.23.0 uses Pydantic model_json_schema() which generates $defs/$ref/anyOf in
  the wire-level tool schemas. AgentCore Gateway rejects these.

  Fix: Wrap mcp._list_tools_mcp to return sanitized schemas, then register the wrapper
  directly on the low-level server: mcp._mcp_server.list_tools()(wrapper).
  This replaces the registered handler without touching any other handlers.
  account_name + region are added to schemas inside the wrapper (no _tools dict access).

Credential injection:
  Patch mcp._tool_manager.call_tool to extract account_name/region, inject AWS credentials
  via os.environ, then delegate to original. try/finally restores env vars to prevent
  credential bleed across sequential requests.

Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html
"""
import asyncio
import copy
import os
import logging

# Set environment variables BEFORE importing the server module.
# awslabs config.py reads these at import time via os.getenv().
os.environ.setdefault('AWS_API_MCP_HOST', '0.0.0.0')  # nosec B104 -- intentional: AgentCore Runtime container networking
os.environ.setdefault('AWS_API_MCP_ALLOWED_HOSTS', '*')
os.environ.setdefault('AWS_API_MCP_ALLOWED_ORIGINS', '*')
os.environ.setdefault('AWS_API_MCP_TRANSPORT', 'streamable-http')
os.environ.setdefault('AWS_API_MCP_STATELESS_HTTP', 'true')
os.environ.setdefault('AUTH_TYPE', 'no-auth')

from mcp import types as mcp_types
from credential_helper import get_customer_session

# Import official AWS Labs server (tools already registered)
# fastmcp 2.14.5 FastMCP instance  uses fastmcp PyPI package, not mcp.server.fastmcp
from awslabs.aws_api_mcp_server.server import server as mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reconfigure for AgentCore Runtime (override their defaults via settings)
mcp.settings.host = "0.0.0.0"  # nosec B104 -- intentional: AgentCore Runtime container networking
mcp.settings.stateless_http = True
logger.info("Configured FastMCP for AgentCore Runtime")

# Banned keywords per AgentCore Gateway docs:
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html
_BANNED = {"$defs", "$anchor", "$schema", "$dynamicRef", "$dynamicAnchor"}


def _sanitize_schema(schema: dict, defs: dict = None) -> dict:
    """
    Sanitize a JSON schema for AgentCore Gateway compatibility.
    - Strips all banned keywords ($defs, $anchor, $schema, $dynamicRef, $dynamicAnchor)
    - Resolves $ref references inline
    - Flattens anyOf/oneOf Optional[T] patterns to simple T
    - Strips Pydantic-generated 'title' fields (noise)
    """
    if not isinstance(schema, dict):
        return schema

    local_defs = dict(defs or {})
    if "$defs" in schema:
        local_defs.update(schema["$defs"])

    def _flatten_optional(node: dict) -> dict:
        for combo in ("anyOf", "oneOf"):
            if combo in node:
                non_null = [v for v in node[combo]
                           if not (isinstance(v, dict) and v.get("type") == "null")]
                if len(non_null) == 1:
                    flat = dict(non_null[0])
                    for k, v in node.items():
                        if k not in (combo, "title"):
                            flat.setdefault(k, v)
                    return flat
        return node

    def _go(node):
        if isinstance(node, list):
            return [_go(i) for i in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                if name in local_defs:
                    resolved = _go(copy.deepcopy(local_defs[name]))
                    siblings = {k: v for k, v in node.items() if k != "$ref"}
                    if siblings:
                        resolved = {**resolved, **siblings}
                    return resolved
            return node
        cleaned = {k: _go(v) for k, v in node.items()
                   if k not in _BANNED and k != "title"}
        return _flatten_optional(cleaned)

    return _go(copy.deepcopy(schema))


# =============================================================================
# Schema sanitization + account_name/region injection via list_tools wrapper.
#
# fastmcp 2.14.5 registers: mcp._mcp_server.list_tools()(mcp._list_tools_mcp)
# We replace just the list_tools handler with our sanitizing wrapper.
# account_name + region are added here rather than modifying internal tool storage
# (fastmcp 2.14.5 has no _tools dict  uses async get_tools() instead).
# =============================================================================
_original_list_tools_mcp = mcp._list_tools_mcp


async def _clean_list_tools():
    """Return sanitized tool schemas + account_name/region for AgentCore Gateway."""
    raw_tools = await _original_list_tools_mcp()
    result = []
    for t in raw_tools:
        schema = dict(t.inputSchema) if t.inputSchema else {}
        clean_schema = _sanitize_schema(schema)

        # Add account_name + region so context_tools.py can inject them
        props = clean_schema.setdefault("properties", {})
        props["account_name"] = {
            "type": "string",
            "description": "Customer account name for cross-account access"
        }
        if "region" not in props:
            props["region"] = {
                "type": "string",
                "description": "AWS region"
            }

        result.append(mcp_types.Tool(
            name=t.name,
            description=t.description,
            inputSchema=clean_schema,
        ))
    logger.info(f"list_tools: returned {len(result)} tools with sanitized schemas + account_name/region")
    return result


# Register our wrapper directly on the low-level server's list_tools handler.
# mcp._mcp_server.list_tools() returns a decorator  calling it with our function
# replaces the registered handler. This is surgical: only list_tools is replaced,
# all other handlers (call_tool, list_resources, etc.) remain as awslabs configured them.
mcp._mcp_server.list_tools()(_clean_list_tools)
logger.info("_clean_list_tools registered on _mcp_server.list_tools handler")

# =============================================================================
# Credential injection via _tool_manager.call_tool patch.
#
# fastmcp 2.14.5 ToolManager.call_tool(key, arguments) is patchable.
# Extract account_name/region from arguments, inject AWS credentials via os.environ,
# delegate to original. try/finally restores env vars to prevent credential bleed.
# =============================================================================
original_call_tool = mcp._tool_manager.call_tool
# Serialize credential inject/call/restore to prevent cross-account credential bleed
# when concurrent requests hit the same container.
_credential_lock = asyncio.Lock()


async def patched_call_tool(key, arguments, **kwargs):
    """Extract account_name/region, save env vars, inject credentials, restore after."""
    account_name = arguments.pop("account_name", None)
    region = arguments.pop("region", "us-east-1")

    # Lock scope covers save->inject->call->restore so the entire lifecycle is atomic.
    async with _credential_lock:
        _cred_keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                      "AWS_REGION", "AWS_DEFAULT_REGION")
        saved_env = {k: os.environ.get(k) for k in _cred_keys}

        try:
            if account_name and account_name != "default":
                session, _ = get_customer_session(account_name, region)
                if session:
                    creds = session.get_credentials().get_frozen_credentials()
                    os.environ["AWS_ACCESS_KEY_ID"] = creds.access_key
                    os.environ["AWS_SECRET_ACCESS_KEY"] = creds.secret_key
                    if creds.token:
                        os.environ["AWS_SESSION_TOKEN"] = creds.token
                    elif "AWS_SESSION_TOKEN" in os.environ:
                        del os.environ["AWS_SESSION_TOKEN"]
                    os.environ["AWS_REGION"] = region
                    os.environ["AWS_DEFAULT_REGION"] = region
                    logger.info(f"Injected credentials for account: {account_name}, region: {region}")
                else:
                    logger.warning(f"Could not get customer session for {account_name!r}, using default credentials")

            return await original_call_tool(key, arguments, **kwargs)

        finally:
            # Restore env vars - prevents credential bleed to next request
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


mcp._tool_manager.call_tool = patched_call_tool
logger.info("call_tool override installed for credential injection (with env var save/restore)")

if __name__ == "__main__":
    mcp.run(transport="streamable-http")