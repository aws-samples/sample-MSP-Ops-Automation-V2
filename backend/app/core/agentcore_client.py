"""
AgentCore SDK Client — central boto3 wrapper for all AgentCore service calls.

Replaces the previous in-process agent manager. Responsibilities:
  - Invoke AgentCore Runtime agents (streaming SSE and non-streaming fallback)
  - Store and query AgentCore Memory (Long-Term / Short-Term)
  - Invoke MCP tools on Runtime endpoints
  - Invoke Gateway tools (external integrations such as Jira)

Singleton lifecycle:
  get_agentcore_client(region) returns a process-wide singleton per region,
  stored in the module-level _client_instances dict. The double-checked locking
  pattern (_client_instances_lock) ensures only one instance is ever created
  even under concurrent async tasks. boto3 clients are thread-safe for reads
  (individual API calls), so no per-call locking is needed once the singleton
  is initialised.

Thread safety notes:
  - boto3.client objects are shared across concurrent coroutines.  This is safe
    because botocore serialises each HTTP request internally.
  - Blocking SDK calls (invoke_agent_runtime, create_event, etc.) are always
    dispatched to a thread-pool executor via loop.run_in_executor() so they
    don't block the asyncio event loop.
  - _stream_lines_to_queue() runs in a background thread and bridges the
    blocking StreamingBody iterator to an asyncio.Queue using
    loop.call_soon_threadsafe() — the only safe way to hand data from a thread
    to a running event loop.
"""
import boto3
import json
import uuid
import logging
import asyncio
import threading
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from botocore.config import Config

logger = logging.getLogger(__name__)

# Module-level singleton — created once per ECS container, reused across all requests.
# Safe because credential injection happens in MCP server processes (not here).
_client_instances: Dict[str, "AgentCoreClient"] = {}
_client_instances_lock = threading.Lock()


def get_agentcore_client(region: str = "us-east-1") -> "AgentCoreClient":
    """Return a cached AgentCoreClient singleton for the given region."""
    if region in _client_instances:
        return _client_instances[region]
    with _client_instances_lock:
        if region not in _client_instances:
            _client_instances[region] = AgentCoreClient(region=region)
            logger.info(f"Created AgentCoreClient singleton for region: {region}")
    return _client_instances[region]


class AgentCoreClient:
    """Client for all AgentCore services"""
    
    def __init__(self, region: str = "us-east-1"):
        self.region = region
        
        # read_timeout=300s (5min): Multi-specialist queries (health check, etc.) call 3+ agents
        # sequentially. Each cold-starts at 60-120s. 3×120s = 360s worst case, so 300s covers
        # most warm + first-cold-start scenarios without hanging forever.
        client_config = Config(
            read_timeout=300,      # 5 minutes for multi-specialist cold-start chains
            connect_timeout=10,    # 10 seconds for connection
            retries={'max_attempts': 1}  # Don't retry long agent calls
        )
        
        self.runtime_client = boto3.client('bedrock-agentcore', region_name=region, config=client_config)
        self.control_client = boto3.client('bedrock-agentcore-control', region_name=region, config=client_config)
    
    async def invoke_runtime(
        self,
        runtime_arn: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Invoke AgentCore Runtime (non-streaming fallback).

        Delegates to invoke_runtime_stream and collects the final 'complete' event.
        This handles both SSE and non-SSE runtimes uniformly, since the supervisor
        now returns SSE from /invocations.
        """
        if not session_id:
            session_id = str(uuid.uuid4())
        try:
            async for evt in self.invoke_runtime_stream(runtime_arn, payload, session_id):
                event_name = evt.get("event", "")
                if event_name == "complete":
                    data = evt.get("data", {})
                    return {
                        "response": data.get("response", ""),
                        "agent_type": data.get("agent_type", "supervisor"),
                        "session_id": session_id,
                        "success": True,
                    }
                elif event_name == "error":
                    return {
                        "response": evt.get("data", {}).get("message", "Agent error"),
                        "agent_type": "error",
                        "session_id": session_id,
                        "success": False,
                    }
            # Stream ended without complete event
            return {"response": "", "agent_type": "error", "session_id": session_id, "success": False}
        except Exception as e:
            logger.error(f"invoke_runtime error: {e}", exc_info=True)
            return {
                "response": "An unexpected error occurred. Please try again.",
                "agent_type": "error",
                "session_id": session_id,
                "success": False,
            }
    
    async def invoke_runtime_stream(
        self,
        runtime_arn: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Invoke AgentCore Runtime with SSE streaming, yielding events as they arrive.

        Iterates the response stream from invoke_agent_runtime using iter_lines(),
        parses SSE events (event:/data: pairs), and yields dicts like:
          {"event": "content", "data": {"text": "chunk", "agent_type": "cloudwatch"}}
          {"event": "agent_switch", "data": {"from_agent": "supervisor", "to_agent": "cloudwatch"}}
          {"event": "tool_call", "data": {"tool_name": "check_cloudwatch"}}
          {"event": "complete", "data": {"response": "...", "agent_type": "..."}}

        Falls back to non-streaming invoke_runtime() if SSE parsing fails.
        """
        if not session_id:
            session_id = str(uuid.uuid4())

        try:
            payload_bytes = json.dumps(payload).encode('utf-8')

            # Blocking boto3 call must run in executor to not block event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.runtime_client.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn,
                    runtimeSessionId=session_id,
                    payload=payload_bytes,
                    qualifier='DEFAULT'
                )
            )

            content_type = response.get("contentType", "")

            if "text/event-stream" not in content_type:
                # Not an SSE stream — fall back to non-streaming
                logger.info("invoke_runtime_stream: non-SSE response, falling back to blob read")
                raw_bytes = b''
                for chunk in response.get("response", []):
                    raw_bytes += chunk
                response_text = raw_bytes.decode('utf-8', errors='replace')
                try:
                    result = json.loads(response_text) if response_text else {}
                except json.JSONDecodeError:
                    result = {"response": response_text}
                yield {"event": "complete", "data": {
                    "response": result.get("response", response_text),
                    "agent_type": result.get("agent_type", "supervisor"),
                }}
                return

            # SSE stream: parse event:/data: pairs from iter_lines()
            # iter_lines() must run in executor as it blocks
            current_event = ""
            current_data = ""

            queue = asyncio.Queue()

            def _stream_lines_to_queue():
                """Read SSE lines from blocking StreamingBody, push to asyncio.Queue.

                IMPORTANT: must use loop.call_soon_threadsafe() — asyncio.Queue is NOT
                thread-safe. put_nowait() called directly from a thread bypasses the
                event loop wakeup, causing await queue.get() to block indefinitely.
                """
                try:
                    for line in response["response"].iter_lines(chunk_size=64):
                        decoded = line.decode("utf-8", errors="replace") if line else ""
                        loop.call_soon_threadsafe(queue.put_nowait, decoded)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

            # Fire-and-forget: we don't await the future here because we consume
            # results through the queue below. The None sentinel posted by the
            # thread signals completion and breaks the while loop.
            loop.run_in_executor(None, _stream_lines_to_queue)

            while True:
                line = await queue.get()
                if line is None:
                    break
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    current_data = line[6:].strip()
                elif line == "" and current_event and current_data:
                    # Complete SSE event — parse and yield
                    try:
                        parsed = json.loads(current_data)
                        yield {"event": current_event, "data": parsed}
                        if current_event == "complete":
                            return
                    except json.JSONDecodeError:
                        logger.warning(f"invoke_runtime_stream: failed to parse SSE data: {current_data[:100]}")
                    current_event = ""
                    current_data = ""

        except Exception as e:
            logger.error(f"invoke_runtime_stream error: {e}")
            yield {"event": "error", "data": {"message": str(e)}}

    async def store_memory(
        self,
        memory_id: str,
        actor_id: str,
        session_id: str,
        events: List[Dict[str, Any]]
    ) -> Dict:
        """
        Persist a list of conversation events to AgentCore Memory (non-blocking).

        Converts the simplified event dicts into the AgentCore conversational
        payload format and calls create_event via a thread-pool executor so the
        asyncio event loop is not blocked. See AgentCore Developer Guide p.280-283.

        Args:
            memory_id:  ARN or ID of the AgentCore Memory resource to write to.
            actor_id:   Identifier for the actor (user/agent) owning these events.
            session_id: Conversation session identifier; groups events for retrieval.
            events:     List of dicts with 'role' and 'content' string fields,
                        e.g. [{"role": "user", "content": "hello"}, ...].

        Returns:
            Dict with keys:
              - 'success' (bool): True if the API call succeeded.
              - 'event_id' (str | None): AgentCore event ID assigned by the service,
                or None on failure.
              - 'error' (str): Present only on failure; contains the ClientError message.
        """
        formatted = [{
            'conversational': {
                'role': e['role'],
                'content': {'text': e['content']}
            }
        } for e in events]
        
        try:
            # Run blocking boto3 call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.runtime_client.create_event(
                    memoryId=memory_id,
                    actorId=actor_id,
                    sessionId=session_id,
                    eventTimestamp=datetime.now(timezone.utc),
                    payload=formatted
                )
            )
            
            return {
                "success": True,
                "event_id": response.get('eventId')
            }
        except ClientError as e:
            logger.error(f"Memory storage failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def query_memory(
        self,
        memory_id: str,
        namespace: str,
        query: str,
        max_results: int = 10
    ) -> List[Dict]:
        """
        Perform a semantic search over AgentCore Memory (non-blocking).

        Delegates to retrieve_memory_records via a thread-pool executor so the
        asyncio event loop is not blocked by the blocking boto3 call.

        Args:
            memory_id:   ARN or ID of the AgentCore Memory resource to search.
            namespace:   Scoping namespace (e.g. account-scoped LTM or session STM).
            query:       Natural-language search string used for semantic similarity.
            max_results: Maximum number of memory record summaries to return (default 10).

        Returns:
            List of memory record summary dicts as returned by the AgentCore API
            (see 'memoryRecordSummaries' in the SDK response). Returns an empty
            list if the call fails.
        """
        try:
            # Run blocking boto3 call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.runtime_client.retrieve_memory_records(
                    memoryId=memory_id,
                    namespace=namespace,
                    searchCriteria={'searchQuery': query},
                    maxResults=max_results
                )
            )
            return response.get('memoryRecordSummaries', [])
        except ClientError as e:
            logger.error(f"Memory query failed: {e}")
            return []
    
    async def invoke_mcp_tool(
        self,
        runtime_arn: str,
        tool: str,
        arguments: Dict[str, Any],
        endpoint_name: str = 'DEFAULT'
    ) -> Dict[str, Any]:
        """
        Invoke a tool on an AgentCore Runtime MCP server (non-blocking).
        
        This method directly calls MCP tools on Runtime without going through
        the Supervisor agent. Use for CloudWatch, AWS API, and AWS Knowledge MCPs.
        """
        try:
            logger.info(f"Invoking MCP tool: {tool} on {runtime_arn.split('/')[-1]}")
            
            # Run blocking boto3 call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.runtime_client.invoke_mcp_tool(
                    agentRuntimeArn=runtime_arn,
                    endpointName=endpoint_name,
                    tool=tool,
                    arguments=arguments
                )
            )
            
            logger.info(f"MCP tool {tool} completed successfully")
            
            return {
                "success": True,
                "output": response.get('output', {}),
                "tool": tool
            }
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            logger.error(f"MCP tool invocation error [{error_code}]: {error_msg}")
            
            return {
                "success": False,
                "error": error_msg,
                "error_code": error_code,
                "tool": tool
            }
        except Exception as e:
            logger.error(f"Unexpected error invoking MCP tool {tool}: {e}")
            return {
                "success": False,
                "error": str(e),
                "tool": tool
            }
    
    async def invoke_gateway_tool(
        self,
        gateway_url: str,
        target_name: str,
        tool: str,
        arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Invoke a tool on AgentCore Gateway target (non-blocking).
        
        Use this for external integrations like Jira that require OAuth.
        Gateway handles authentication and protocol translation.
        """
        try:
            logger.info(f"Invoking Gateway tool: {tool} on target {target_name}")
            
            # Run blocking boto3 call in thread pool
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.runtime_client.invoke_mcp_tool(
                    gatewayUrl=gateway_url,
                    targetName=target_name,
                    tool=tool,
                    arguments=arguments
                )
            )
            
            logger.info(f"Gateway tool {tool} completed successfully")
            
            return {
                "success": True,
                "output": response.get('output', {}),
                "tool": tool,
                "target": target_name
            }
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            logger.error(f"Gateway tool invocation error [{error_code}]: {error_msg}")
            
            return {
                "success": False,
                "error": error_msg,
                "error_code": error_code,
                "tool": tool,
                "target": target_name
            }
        except Exception as e:
            logger.error(f"Unexpected error invoking Gateway tool {tool}: {e}")
            return {
                "success": False,
                "error": str(e),
                "tool": tool,
                "target": target_name
            }
