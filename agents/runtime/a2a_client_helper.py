"""A2A client helper for supervisor → specialist communication.

Uses boto3 invoke_agent_runtime (SigV4 automatic) per the AgentCore dev guide.

Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke.html
"""
import json
import os
import logging
import time
from uuid import uuid4

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Load KEY=VALUE pairs from an env config file into the process environment.

    AgentCore Runtime containers launch the Python process without sourcing the
    shell profile, so environment variables written by deploy.sh into env_config.txt
    are not present in os.environ at startup. This function bridges that gap by
    reading the file and calling os.environ.setdefault — which means already-set
    variables (e.g. injected by the container runtime itself) are never overwritten.

    Search order: env_config.txt is checked first (the deploy.sh-generated file),
    then .env as a developer-local fallback. The loop breaks after the first match
    so only one file is ever loaded.

    Args:
        None — reads from the same directory as this module (__file__).

    Returns:
        None — side effect only: mutates os.environ.
    """
    for name in ['env_config.txt', '.env']:
        env_path = os.path.join(os.path.dirname(__file__), name)
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, val = line.split('=', 1)
                        os.environ.setdefault(key.strip(), val.strip())
            break

_load_env_file()

AGENT_ARNS = {
    "cloudwatch": os.getenv("CLOUDWATCH_A2A_ARN"),
    "security": os.getenv("SECURITY_A2A_ARN"),
    "cost": os.getenv("COST_A2A_ARN"),
    "advisor": os.getenv("ADVISOR_A2A_ARN"),
    "knowledge": os.getenv("KNOWLEDGE_A2A_ARN"),
    "jira": os.getenv("JIRA_A2A_ARN"),
}

# Startup debug logging to verify env file loading
logger.info("=" * 60)
logger.info("A2A Client Helper Initialization")
logger.info("=" * 60)
for agent_key, arn in AGENT_ARNS.items():
    if arn:
        logger.info(f"  {agent_key:12s}: {arn}")
    else:
        logger.warning(f"  {agent_key:12s}: NOT CONFIGURED")
logger.info("=" * 60)

_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
_client = boto3.client(
    "bedrock-agentcore",
    region_name=_REGION,
    config=Config(
        read_timeout=120,  # 120s for slow MCP tool chains (Cost Explorer, Security Hub can take 60-130s)
        connect_timeout=10
    )
)


def send_to_agent_sync(
    agent_key: str,
    prompt: str,
    account_name: str,
    region: str,
) -> str:
    """Send a message to a specialist agent via invoke_agent_runtime (SigV4).
    
    Includes retry logic with exponential backoff to handle ConcurrencyException
    when the agent is already processing another request.

    Args:
        agent_key: One of cloudwatch, security, cost, advisor, knowledge, jira
        prompt: The user's question
        account_name: Customer account name (or "default" for MSP account)
        region: AWS region

    Returns:
        str: The agent's text response
    """
    arn = AGENT_ARNS.get(agent_key)
    if not arn:
        logger.error(f"No ARN configured for agent '{agent_key}'")
        return f"Error: No ARN configured for agent '{agent_key}'. Check environment variables."

    # Two retry strategies based on how quickly the error returned:
    #
    # 1. INSTANT error (< 5s) → ConcurrencyException: agent is busy with previous request.
    #    Use LONG backoff [30s, 60s, 90s] to wait for the current request to finish.
    #    A2A agents process one request at a time (Strands SDK constraint).
    #
    # 2. SLOW error (>= 5s) → Cold start / infrastructure issue.
    #    Use SHORT backoff [10s, 20s, 40s] for quick recovery.
    #
    # This ensures zero failures: either the agent finishes and we succeed,
    # or we genuinely cannot reach it and report a real error.
    MAX_RETRIES = 3
    COLD_START_BACKOFF = [10, 20, 40]   # For slow errors (cold start, network)
    BUSY_BACKOFF = [30, 60, 90]          # For instant errors (ConcurrencyException)
    
    session_id = str(uuid4())
    logger.info(f"Invoking {agent_key} agent")
    logger.info(f"   ARN: {arn}")
    logger.info(f"   account_name: {account_name!r}")
    logger.info(f"   region: {region}")
    logger.info(f"   prompt (first 100 chars): {prompt[:100]}...")

    # Prepend metadata as JSON line — context_tools.py extracts it before LLM sees the prompt
    import json as _json
    meta_prefix = _json.dumps({"__metadata__": {"account_name": account_name, "region": region}})
    full_prompt = f"{meta_prefix}\n{prompt}"
    
    logger.info(f"   Metadata prefix: {meta_prefix}")
    logger.info(f"   Full prompt length: {len(full_prompt)} chars")

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

    last_error = None
    busy_retry_count = 0   # Track busy-agent retries separately

    for attempt in range(MAX_RETRIES + 1):  # 0, 1, 2, 3 = 4 total attempts
        try:
            invoke_start = time.time()
            response = _client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                runtimeSessionId=session_id,
                payload=payload,
            )
            invoke_elapsed = time.time() - invoke_start

            content_type = response.get("contentType", "")

            # Collect raw response bytes
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

            # Parse A2A JSON-RPC response — extract text from result.parts
            result = ""
            has_error = False
            try:
                data = json.loads(raw_text) if raw_text else {}
                
                # Check for A2A error response
                if "error" in data:
                    has_error = True
                    error_msg = data["error"].get("message", "Unknown error")
                    logger.warning(f"A2A error response: {error_msg}")
                    result = f"Error: {error_msg}"
                elif "result" in data:
                    for part in data["result"].get("parts", []):
                        if part.get("kind") == "text":
                            result += part["text"]
                        elif "text" in part:
                            result += part["text"]
                if not result and not has_error:
                    result = raw_text  # fallback to raw
            except (json.JSONDecodeError, KeyError):
                result = raw_text

            # ConcurrencyException: A2A server wraps it as "Internal error" before sending.
            # Detect by response speed: instant (<5s) errors are almost always concurrency.
            # The 5-second threshold is intentionally conservative — a genuine cold-start
            # or network timeout takes at least 10s, so anything faster is almost certainly
            # the agent rejecting the request immediately due to a busy lock.
            is_busy_error = has_error and invoke_elapsed < 5.0

            if is_busy_error:
                if busy_retry_count < len(BUSY_BACKOFF):
                    wait_time = BUSY_BACKOFF[busy_retry_count]
                    busy_retry_count += 1
                    logger.warning(
                        f"Agent {agent_key} busy (responded in {invoke_elapsed:.1f}s — likely ConcurrencyException). "
                        f"Waiting {wait_time}s for current request to complete... "
                        f"(busy retry {busy_retry_count}/{len(BUSY_BACKOFF)})"
                    )
                    time.sleep(wait_time)
                    continue
                else:
                    # Exhausted busy retries — agent has been busy for 30+60+90=180s
                    logger.error(f"Agent {agent_key} remained busy after {sum(BUSY_BACKOFF)}s total wait")
                    return f"Error: {agent_key} agent is still processing a previous request after waiting {sum(BUSY_BACKOFF)} seconds."

            # Standard retryable error (cold start, network issue — slow response)
            is_retryable = (
                has_error or
                "Internal error" in result or
                not result
            )

            if is_retryable and attempt < MAX_RETRIES:
                wait_time = COLD_START_BACKOFF[min(attempt, len(COLD_START_BACKOFF) - 1)]
                logger.warning(f"Agent error after {invoke_elapsed:.1f}s (attempt {attempt + 1}/{MAX_RETRIES + 1})")
                logger.warning(f"   Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            
            # Success or final attempt
            if not result:
                logger.warning(f"No response from {agent_key} agent")
                return f"Agent {agent_key} returned no response"

            logger.info(f"Response from {agent_key} ({len(str(result))} chars)")
            logger.info(f"{agent_key} response preview: {str(result)[:1000]}")
            return str(result)

        except Exception as e:
            last_error = e
            logger.error(f"Error invoking {agent_key} agent (attempt {attempt + 1}/{MAX_RETRIES + 1}): {e}")
            
            if attempt < MAX_RETRIES:
                wait_time = COLD_START_BACKOFF[min(attempt, len(COLD_START_BACKOFF) - 1)]
                logger.warning(f"   Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            
            # Final attempt failed
            return f"Error invoking {agent_key} agent after {MAX_RETRIES + 1} attempts: {str(last_error)}"
    
    # Should not reach here, but just in case
    return f"Error invoking {agent_key} agent: Maximum retries exceeded"
