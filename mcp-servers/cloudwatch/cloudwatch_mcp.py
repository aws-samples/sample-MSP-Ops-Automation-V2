"""
CloudWatch MCP Server - Thin Wrapper for Official AWS Labs Package
===================================================================
Wraps awslabs.cloudwatch-mcp-server with cross-account credential injection.

Official package provides tools via 3 tool classes:
  CloudWatchAlarmsTools, CloudWatchLogsTools, CloudWatchMetricsTools

Credential injection: Intercepts call_tool to extract account_name, set env vars,
then delegate to official tool code which reads credentials from boto3.Session().

Schema sanitization for AgentCore Gateway compatibility:
  mcp>=1.23.0 (required by awslabs packages) uses Pydantic model_json_schema()
  which generates $defs/$ref/anyOf in the wire-level tool schemas.
  AgentCore Gateway rejects $defs/$ref and fails to sync targets.

  Fix: patch mcp_server.list_tools (the async FastMCP method) to return new
  mcp.types.Tool objects with sanitized schemas. This intercepts the wire-level
  output AFTER Pydantic serialization, so the clean dict is what Gateway sees.

Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html
"""
import asyncio
import copy
import os
import logging
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from credential_helper import get_customer_session

# Import official AWS Labs tool classes
from awslabs.cloudwatch_mcp_server.cloudwatch_alarms.tools import CloudWatchAlarmsTools
from awslabs.cloudwatch_mcp_server.cloudwatch_logs.tools import CloudWatchLogsTools
from awslabs.cloudwatch_mcp_server.cloudwatch_metrics.tools import CloudWatchMetricsTools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


# Create OUR FastMCP instance with AgentCore Runtime requirements
mcp = FastMCP(
    name="cloudwatch-mcp",
    host="0.0.0.0",  # nosec B104 -- intentional: AgentCore Runtime container networking
    stateless_http=True
)

# Register official AWS Labs tools
try:
    cloudwatch_logs_tools = CloudWatchLogsTools()
    cloudwatch_logs_tools.register(mcp)
    logger.info("CloudWatch Logs tools registered")

    cloudwatch_metrics_tools = CloudWatchMetricsTools()
    cloudwatch_metrics_tools.register(mcp)
    logger.info("CloudWatch Metrics tools registered")

    cloudwatch_alarms_tools = CloudWatchAlarmsTools()
    cloudwatch_alarms_tools.register(mcp)
    logger.info("CloudWatch Alarms tools registered")
except Exception as e:
    logger.warning(f"Error registering tools: {e}")
    raise

# Add account_name to every tool's schema so context_tools.py injects it
for tool in mcp._tool_manager._tools.values():
    tool.parameters["properties"]["account_name"] = {
        "type": "string",
        "description": "Customer account name for cross-account access"
    }
logger.info(f"Added account_name to {len(mcp._tool_manager._tools)} tool schemas")

# =============================================================================
# Patch mcp.list_tools (the async FastMCP method) to return sanitized schemas.
#
# mcp>=1.23.0 (required by awslabs packages) calls model_dump_json() on Pydantic
# models which re-introduces $defs/$ref/$anchor at the wire level even after
# tool.parameters is cleaned. The fix is to intercept AFTER Pydantic serialization
# by patching mcp.list_tools to return new mcp.types.Tool objects with clean schemas.
#
# This survives mcp.run() because we patch the FastMCP instance method, not the
# low-level server handler (which gets re-registered by setup_handlers).
# =============================================================================
_original_list_tools = mcp.list_tools


async def _clean_list_tools():
    """Return sanitized tool schemas for AgentCore Gateway compatibility."""
    raw_tools = await _original_list_tools()
    result = []
    for t in raw_tools:
        clean_schema = _sanitize_schema(dict(t.inputSchema) if t.inputSchema else {})
        result.append(mcp_types.Tool(
            name=t.name,
            description=t.description,
            inputSchema=clean_schema,
        ))
    logger.info(f"list_tools: returned {len(result)} tools with sanitized schemas")
    return result


mcp.list_tools = _clean_list_tools
# Re-run _setup_handlers() to register the patched list_tools on the low-level MCP server.
#
# Why this is necessary:
#   FastMCP.__init__ calls _setup_handlers() once during object construction, binding
#   the low-level server's "tools/list" handler to the ORIGINAL mcp.list_tools method.
#   Replacing mcp.list_tools on the instance (line above) updates the FastMCP attribute
#   but does NOT update the already-bound low-level handler — it still points at the
#   original.  Calling _setup_handlers() a second time forces re-registration of all
#   handlers using the current attribute values, so "tools/list" now routes through
#   _clean_list_tools when a client (or AgentCore Gateway) calls it.
#
#   This is safe to call more than once: the handlers are idempotent registrations.
mcp._setup_handlers()
logger.info("mcp.list_tools patched and re-registered on low-level server")

# Override call_tool to inject credentials with safe save/restore of env vars.
# The try/finally ensures that even if the tool raises an exception, the process
# env vars are restored to prevent credential bleed across sequential requests.
original_call_tool = mcp._tool_manager.call_tool
# Serialize credential inject/call/restore to prevent cross-account credential bleed
# when concurrent requests hit the same container.
_credential_lock = asyncio.Lock()


async def patched_call_tool(key, arguments, **kwargs):
    """Extract account_name, save env vars, inject credentials, restore after."""
    account_name = arguments.pop("account_name", None)
    region = arguments.get("region", "us-east-1")

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
                    logger.info(f"Injected credentials for account: {account_name}")
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