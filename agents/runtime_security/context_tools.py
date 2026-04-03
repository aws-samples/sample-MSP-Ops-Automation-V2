"""Wrap MCP tools to auto-inject account_id/region from A2A message metadata.

Instead of wrapping MCPAgentTool (which breaks Strands Agent tool registry
isinstance checks), we monkey-patch each tool's stream() method in-place.
The tool remains a valid MCPAgentTool instance throughout.

Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-agent-integration.html
     Official pattern: tools = mcp_client.list_tools_sync(); Agent(tools=tools)

Usage in specialist runtimes:
    from context_tools import create_context_agent, create_a2a_server
"""
import json
import logging
import os
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

logger = logging.getLogger(__name__)

MODEL = os.getenv('MODEL_ID', os.getenv('MODEL', 'global.anthropic.claude-sonnet-4-20250514-v1:0'))
MAX_TOKENS = int(os.getenv('MAX_TOKENS', '4096'))
# Module-level context — set before each agent invocation
_current_ctx = {"account_name": "", "region": "us-east-1"}


def set_context(account_name: str, region: str = "us-east-1"):
    """Set current account context (called before agent invocation)."""
    global _current_ctx
    _current_ctx = {"account_name": account_name, "region": region}
    logger.info(f"Context set: account_name={account_name!r}, region={region}")
    logger.info(f"   This context will be injected into all MCP tool calls")


def _inject_context_into_tools(tools):
    """Monkey-patch each MCPAgentTool's stream() to auto-inject account_id/region.

    The tool object stays the same MCPAgentTool instance (passes isinstance checks).
    Only the stream method is replaced with one that injects context before delegating.
    """
    patched_count = 0
    for tool in tools:
        props = tool.tool_spec.get('inputSchema', {}).get('json', {}).get('properties', {})
        if 'account_name' not in props and 'region' not in props:
            continue  # Tool doesn't accept context params, skip

        patched_count += 1
        original_stream = tool.stream
        accepts_account = 'account_name' in props
        accepts_region = 'region' in props

        def _make_patched(orig, acc, reg):
            def patched_stream(tool_use, invocation_state=None, **kwargs):
                tool_input = tool_use.get("input", {})
                tool_name = tool_use.get("name", "unknown")
                
                # Log before injection
                logger.info(f"Tool call intercepted: {tool_name}")
                logger.info(f"   Original input: {json.dumps(tool_input, default=str)}")
                
                if acc and "account_name" not in tool_input:
                    tool_input["account_name"] = _current_ctx["account_name"]
                    logger.info(f"  Injected account_name={_current_ctx['account_name']!r}")
                if reg and "region" not in tool_input:
                    tool_input["region"] = _current_ctx["region"]
                    logger.info(f"  Injected region={_current_ctx['region']}")
                
                tool_use["input"] = tool_input
                logger.info(f"   Final input: {json.dumps(tool_input, default=str)}")
                
                return orig(tool_use, invocation_state, **kwargs)
            return patched_stream

        tool.stream = _make_patched(original_stream, accepts_account, accepts_region)

    logger.info(f"Patched {patched_count}/{len(tools)} tools with context injection")


def _extract_metadata_prompt(original_prompt: str) -> str:
    """Extract metadata JSON prefix from prompt if present, set context, return clean prompt."""
    try:
        if original_prompt.startswith('{"__metadata__":'):
            nl_pos = original_prompt.find('\n')
            if nl_pos == -1:
                logger.warning("Metadata prefix found but no newline delimiter, using prompt as-is")
                return original_prompt
            meta_line = original_prompt[:nl_pos]
            meta = json.loads(meta_line).get("__metadata__", {})

            logger.info("Extracted metadata from prompt")
            logger.info(f"   Raw metadata: {meta_line}")
            logger.info(f"   account_name: {meta.get('account_name', 'NOT SET')!r}")
            logger.info(f"   region: {meta.get('region', 'us-east-1')}")

            set_context(meta.get("account_name", ""), meta.get("region", "us-east-1"))
            clean_prompt = original_prompt[nl_pos + 1:]
            logger.info(f"   Clean prompt (first 100 chars): {clean_prompt[:100]}...")
            return clean_prompt
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to parse metadata prefix: {e}, using original prompt")
        return original_prompt

    logger.info("No metadata prefix found in prompt, using defaults")
    return original_prompt


def create_context_agent(name, description, system_prompt, mcp_client, max_tokens=None):
    """Create an Agent with context-injected MCP tools.

    Follows the official Strands + Gateway pattern:
      tools = mcp_client.list_tools_sync()
      Agent(tools=tools)
    but patches stream() on each tool to inject account_id/region.

    Args:
        name: Agent name
        description: Agent description
        system_prompt: System prompt for the agent
        mcp_client: Connected MCPClient instance
        max_tokens: Maximum output tokens. Defaults to MAX_TOKENS env var (default 4096).
    """
    tools = mcp_client.list_tools_sync()
    _inject_context_into_tools(tools)

    return Agent(
        name=name,
        description=description,
        model=BedrockModel(model_id=MODEL, max_tokens=max_tokens or MAX_TOKENS),
        tools=tools,
        system_prompt=system_prompt,
        callback_handler=None,
    )


def create_a2a_server(agent, runtime_url):
    """Create A2AServer with metadata extraction hook."""
    original_stream = agent.stream_async

    async def patched_stream(content_blocks, **kwargs):
        if content_blocks:
            block = content_blocks[0]
            if hasattr(block, 'text'):
                block.text = _extract_metadata_prompt(block.text)
            elif isinstance(block, dict) and 'text' in block:
                block['text'] = _extract_metadata_prompt(block['text'])
        async for event in original_stream(content_blocks, **kwargs):
            yield event

    agent.stream_async = patched_stream
    return A2AServer(agent=agent, http_url=runtime_url, serve_at_root=True)
