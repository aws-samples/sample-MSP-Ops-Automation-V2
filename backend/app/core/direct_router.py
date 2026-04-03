"""
Direct A2A Router — bypasses Supervisor for known-domain queries.

For queries where the domain is unambiguous (cost, cloudwatch, security, advisor),
this client invokes the specialist A2A runtime directly, skipping the Supervisor
LLM hop entirely. This saves 45-90s per request.

Context passing:
  Uses the SAME metadata JSON prefix pattern as a2a_client_helper.py:
    {"__metadata__": {"account_name": ..., "region": ...}}\n{prompt}
  This ensures context_tools.py._extract_metadata_prompt() correctly extracts
  account context and calls set_context() before any MCP tool calls.
  Credential injection in MCP servers is unaffected.

Fallback:
  If the specialist ARN is not configured, returns None so the caller
  falls back to Supervisor routing.
"""
import os
import json
import uuid
import logging
import asyncio
import threading
from typing import Dict, Any, Optional

from botocore.config import Config
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Specialist ARN map — populated from env vars written by deploy.sh Step 9
SPECIALIST_ARN_MAP: Dict[str, str] = {
    "cloudwatch": os.getenv("CLOUDWATCH_A2A_ARN", ""),
    "security":   os.getenv("SECURITY_A2A_ARN", ""),
    "cost":       os.getenv("COST_A2A_ARN", ""),
    "advisor":    os.getenv("ADVISOR_A2A_ARN", ""),
    "jira":       os.getenv("JIRA_A2A_ARN", ""),
    "knowledge":  os.getenv("KNOWLEDGE_A2A_ARN", ""),
}

# Module-level singleton
_direct_router_instance: Optional["DirectRouterClient"] = None
_direct_router_lock = threading.Lock()


def get_direct_router() -> "DirectRouterClient":
    """Return singleton DirectRouterClient."""
    global _direct_router_instance
    if _direct_router_instance is not None:
        return _direct_router_instance
    with _direct_router_lock:
        if _direct_router_instance is None:
            _direct_router_instance = DirectRouterClient()
            configured = sum(1 for v in SPECIALIST_ARN_MAP.values() if v)
            logger.info(f"DirectRouterClient initialized ({configured}/{len(SPECIALIST_ARN_MAP)} ARNs configured)")
    return _direct_router_instance


class DirectRouterClient:
    """
    Invokes A2A specialist runtimes directly from the backend,
    bypassing the Supervisor LLM for known-domain queries.
    
    Uses the same invoke_agent_runtime API as AgentCoreClient but
    formats the payload as A2A JSON-RPC (same as a2a_client_helper.py).
    """

    def __init__(self):
        region = os.getenv("AWS_REGION", "us-east-1")
        self.region = region
        
        # Reuse reduced timeout from agentcore_client.py
        client_config = Config(
            read_timeout=180,
            connect_timeout=10,
            retries={"max_attempts": 1}
        )
        self._boto_client = boto3.client(
            "bedrock-agentcore",
            region_name=region,
            config=client_config
        )

    def can_route_directly(self, agent_hint: str) -> bool:
        """Return True if a configured ARN exists for this agent hint.

        Args:
            agent_hint: Domain key — one of cloudwatch, security, cost, advisor,
                        jira, knowledge.

        Returns:
            True if the corresponding env-var ARN was set at startup (non-empty
            string); False if the ARN is missing, meaning the caller should fall
            back to Supervisor routing.
        """
        arn = SPECIALIST_ARN_MAP.get(agent_hint, "")
        return bool(arn)

    async def invoke_specialist(
        self,
        agent_key: str,
        prompt: str,
        account_name: str,
        region: str,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Invoke a specialist A2A runtime directly.

        Args:
            agent_key: One of cloudwatch, security, cost, advisor, jira, knowledge
            prompt: User's question (plain text, no metadata prefix needed here)
            account_name: Customer account name (or "default" for MSP account)
            region: AWS region
            session_id: Optional session ID for conversation continuity

        Returns:
            Dict with 'response' and 'agent_type' keys, or None if ARN not configured.
        """
        arn = SPECIALIST_ARN_MAP.get(agent_key, "")
        if not arn:
            logger.warning(f"DirectRouter: No ARN for '{agent_key}', falling back to Supervisor")
            return None

        if not session_id:
            session_id = str(uuid.uuid4())

        # Enrich prompt with Jira config when routing directly (supervisor path
        # does this in supervisor_tools.py; direct path must do it here)
        if agent_key == "jira":
            jira_project_key = os.getenv("JIRA_PROJECT_KEY", "")
            jira_domain = os.getenv("JIRA_DOMAIN", "")
            jira_email = os.getenv("JIRA_EMAIL", "")
            jira_ctx = (
                f"\n\n[Jira Config — use these values, never ask the user]\n"
                f"project_key: {jira_project_key}\n"
                f"domain: {jira_domain}\n"
                f"email: {jira_email}\n"
            )
            prompt = prompt + jira_ctx

        # Prepend metadata JSON prefix — same pattern as a2a_client_helper.py
        # context_tools.py._extract_metadata_prompt() reads this to set account context
        meta_prefix = json.dumps({"__metadata__": {"account_name": account_name, "region": region}})
        full_prompt = f"{meta_prefix}\n{prompt}"

        # A2A JSON-RPC payload — same format as a2a_client_helper.py
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": session_id,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": full_prompt}],
                    "messageId": session_id,
                }
            }
        }).encode()

        try:
            logger.info(f"DirectRouter: Invoking {agent_key} specialist directly")
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._boto_client.invoke_agent_runtime(
                    agentRuntimeArn=arn,
                    runtimeSessionId=session_id,
                    payload=payload,
                )
            )

            content_type = response.get("contentType", "")

            # Collect raw response bytes (same logic as a2a_client_helper.py)
            raw_chunks = []
            if "text/event-stream" in content_type:
                for line in response["response"].iter_lines(chunk_size=10):
                    if line:
                        decoded = line.decode("utf-8")
                        if decoded.startswith("data: "):
                            raw_chunks.append(decoded[6:])
                raw_text = "\n".join(raw_chunks)
            else:
                raw_bytes = b"".join(response.get("response", []))
                raw_text = raw_bytes.decode("utf-8") if raw_bytes else ""

            # Parse A2A JSON-RPC response.
            #
            # The A2A spec (current) returns text in a nested artifacts structure:
            #   {"result": {"artifacts": [{"parts": [{"kind":"text","text":"..."}]}]}}
            #
            # Older specialist runtimes (pre-spec) return a flat parts structure:
            #   {"result": {"parts": [{"kind":"text","text":"..."}]}}
            #
            # We try artifacts first; if that yields nothing we fall back to flat
            # parts.  Both paths check for "kind":"text" first (strict A2A) then
            # fall back to any dict containing a "text" key (defensive).
            #
            # If JSON parsing fails entirely (e.g. the runtime returned plain text),
            # raw_text is used verbatim.
            result_text = ""
            try:
                data = json.loads(raw_text) if raw_text else {}
                if "result" in data:
                    result = data["result"]

                    # Primary: artifacts[].parts[] (A2A spec format)
                    artifacts = result.get("artifacts", [])
                    for artifact in artifacts:
                        for part in artifact.get("parts", []):
                            if part.get("kind") == "text":
                                result_text += part["text"]
                            elif "text" in part:
                                result_text += part["text"]

                    # Fallback: result.parts[] (legacy format)
                    if not result_text:
                        for part in result.get("parts", []):
                            if part.get("kind") == "text":
                                result_text += part["text"]
                            elif "text" in part:
                                result_text += part["text"]

                if not result_text:
                    result_text = raw_text
            except (json.JSONDecodeError, KeyError):
                result_text = raw_text

            if not result_text:
                logger.warning(f"DirectRouter: Empty response from {agent_key}")
                return None

            logger.info(f"DirectRouter: {agent_key} responded ({len(result_text)} chars)")
            return {
                "response": result_text,
                "agent_type": agent_key,
                "session_id": session_id,
                "direct_routed": True,
            }

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_msg = e.response["Error"]["Message"]
            logger.error(f"DirectRouter: {agent_key} ClientError [{error_code}]: {error_msg}")
            # Return None to trigger Supervisor fallback
            return None
        except Exception as e:
            logger.error(f"DirectRouter: {agent_key} unexpected error: {e}")
            return None