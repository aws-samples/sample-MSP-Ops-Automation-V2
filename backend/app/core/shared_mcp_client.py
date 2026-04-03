"""
SharedMCPClient — No-op stub for AgentCore Runtime architecture.

MCP clients now run inside AgentCore Runtime containers (not the backend).
This stub satisfies imports in main.py (startup preload) and workspace_context.py
(credential cache invalidation) without doing any actual MCP client management.

Previously, MCP clients ran in-process in the backend. After the AgentCore migration,
all MCP tool calls go through:
  - Gateway HTTP calls (workflow_graph.py → _call_gateway_tool)
  - A2A specialist runtimes (direct_router.py → invoke_specialist)
  - Supervisor runtime (agentcore_client.py → invoke_runtime)
"""
import logging

logger = logging.getLogger(__name__)


class SharedMCPClient:
    """No-op MCP client manager — agents run in AgentCore Runtime, not backend."""

    @classmethod
    def initialize(cls):
        """Called by main.py on startup. No-op since MCP runs in Runtime containers."""
        logger.info("SharedMCPClient.initialize() — no-op (agents run in AgentCore Runtime)")

    @classmethod
    def clear_customer_cache(cls, account_name: str = None):
        """Called by workspace_context.py on account switch. No-op since MCP runs in Runtime."""
        if account_name:
            logger.debug(f"SharedMCPClient.clear_customer_cache({account_name}) — no-op")
        else:
            logger.debug("SharedMCPClient.clear_customer_cache() — no-op")