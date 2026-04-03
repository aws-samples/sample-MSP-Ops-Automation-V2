#!/usr/bin/env python3
"""Supervisor Runtime — HTTP protocol on port 8080.

This is the ONLY runtime the backend talks to directly via HTTP.
It delegates to A2A specialist runtimes via tools.

Refs:
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/response-streaming.html
- HTTP protocol: port 8080, path /invocations
"""
import json
import logging
import os
import uuid
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from supervisor_agent import create_supervisor_agent
from supervisor_tools import set_context, get_last_tool_called, get_all_tools_called, reset_tools_called, clear_tools_state

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="MSP Ops Supervisor Runtime",
    description="Coordinates specialist A2A agents for MSP operations"
)

# Memory configuration — per AgentCore Memory API docs
MEMORY_ID = os.getenv("MEMORY_ID")
memory_client = None

if MEMORY_ID:
    try:
        from bedrock_agentcore.memory import MemoryClient
        memory_client = MemoryClient(region_name=os.getenv("AWS_REGION", "us-east-1"))
        logger.info(f"Memory client initialized: {MEMORY_ID}")
    except Exception as e:
        logger.warning(f"Memory client initialization failed: {e}")
        memory_client = None

# Cached SemanticFacts strategy ID — resolved once on first LTM query
_semantic_strategy_id = None


def _get_semantic_strategy_id() -> str:
    """Lazy-resolve the SemanticFacts strategy ID; returns '' if unavailable."""
    global _semantic_strategy_id
    if _semantic_strategy_id is not None:
        return _semantic_strategy_id
    if not MEMORY_ID or not memory_client:
        return ""
    try:
        strategies = memory_client.get_memory_strategies(MEMORY_ID)
        for s in strategies:
            if s.get("name") == "SemanticFacts":
                _semantic_strategy_id = s.get("strategyId", s.get("memoryStrategyId", ""))
                if _semantic_strategy_id:
                    logger.info(f"SemanticFacts strategy ID resolved: {_semantic_strategy_id}")
                break
    except Exception as e:
        logger.warning(f"Failed to resolve SemanticFacts strategy ID: {e}")
    return _semantic_strategy_id or ""


def _truncate_for_context(content: str, role: str) -> str:
    """Truncate a conversation turn for context — longer budget for assistant responses.

    Asymmetric budget rationale: user messages tend to be short questions (200 chars
    is generous), while assistant responses carry the substantive answers the model
    needs to recall (800 chars preserves enough detail to avoid re-fetching data).
    Truncating at a sentence boundary (rfind '. ') keeps the snippet readable and
    prevents the model from seeing a mid-sentence cut that could cause misinterpretation.
    The 0.6 threshold ensures we only sentence-break if a period exists in the latter
    half of the window — otherwise the plain character limit is used.
    """
    max_len = 200 if role.upper() == "USER" else 800
    if len(content) <= max_len:
        return content
    truncated = content[:max_len]
    last_period = truncated.rfind('. ')
    if last_period > max_len * 0.6:
        truncated = truncated[:last_period + 1]
    return truncated + " ... [truncated]"


# Create supervisor agent on startup (singleton for all requests)
supervisor = create_supervisor_agent()


def _load_memory_context(actor_id: str, session_id: str) -> str:
    """
    Load the last 3 conversation turns from AgentCore Memory and return them
    as a context prefix string to prepend to the user prompt.
    Returns an empty string if memory is not configured or the load fails.
    """
    if not MEMORY_ID or not memory_client:
        return ""
    try:
        turns = memory_client.get_last_k_turns(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=3,
        )
        if not turns:
            logger.info(f"No previous conversation in Memory for session {session_id[:16]}...")
            return ""
        context_lines = []
        for turn in turns:
            for msg in turn:
                role = msg.get("role", "")
                content = msg.get("content", {}).get("text", "")
                content = _truncate_for_context(content, role)
                context_lines.append(f"{role}: {content}")
        logger.info(f"Loaded {len(turns)} turns from Memory for session {session_id[:16]}...")
        return "Previous conversation:\n" + "\n".join(context_lines) + "\n\n"
    except Exception as e:
        logger.warning(f"Failed to load conversation from Memory: {e}")
        return ""


def _load_ltm_context(query: str, actor_id: str, account_name: str = "default") -> str:
    """Query SemanticFacts LTM for facts relevant to this query."""
    strategy_id = _get_semantic_strategy_id()
    if not strategy_id or not MEMORY_ID or not memory_client:
        return ""
    try:
        namespace = f"/strategy/{strategy_id}/actor/{actor_id}/account/{account_name}/"
        records = memory_client.retrieve_memories(
            memory_id=MEMORY_ID,
            namespace=namespace,
            query=query,
            top_k=5,
        )
        if not records:
            return ""
        facts = [r.get("content", {}).get("text", "") for r in records]
        facts = [f for f in facts if f]
        if not facts:
            return ""
        logger.info(f"LTM retrieved {len(facts)} facts for actor {actor_id[:8]}...")
        return "Relevant context from previous conversations:\n" + "\n".join(f"- {f}" for f in facts[:5]) + "\n\n"
    except Exception as e:
        logger.warning(f"LTM retrieval failed: {e}")
        return ""


def _save_memory_turn(actor_id: str, session_id: str, prompt: str, response: str) -> None:
    """Persist a completed USER/ASSISTANT turn to AgentCore Memory."""
    if not MEMORY_ID or not memory_client:
        return
    try:
        memory_client.create_event(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            messages=[(prompt, "USER"), (response, "ASSISTANT")],
        )
        logger.info(f"Saved conversation turn to Memory for session {session_id[:16]}...")
    except Exception as e:
        logger.warning(f"Failed to save conversation to Memory: {e}")


@app.post("/invocations")
async def invoke(request: Request):
    """Handle invocations from backend — returns SSE StreamingResponse.

    AgentCore SDK calls this endpoint. By returning text/event-stream,
    the boto3 response contentType becomes 'text/event-stream' and the
    backend's invoke_runtime_stream() enters the SSE parsing path,
    enabling real-time token-by-token streaming to the frontend.
    """
    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        context = body.get("context", {})
        account_name = body.get("account_name") or context.get("account_name", "default")
        region = body.get("region") or context.get("region", "us-east-1")
        session_id = body.get("session_id", "default")
        user_context = body.get("user_context", {})
        actor_id = user_context.get("user_id", "default")

        logger.info(f"Received invocation: account_name={account_name}, region={region}, session={session_id[:16]}..., prompt={prompt[:50]}...")

        # STM (Short-Term Memory): last 3 conversation turns scoped to this session_id.
        # Gives the agent conversational continuity within the current chat session.
        stm_context = _load_memory_context(actor_id, session_id)
        # LTM (Long-Term Memory): semantic facts retrieved across all sessions for this
        # actor + account. Provides durable context (e.g. recurring issues, preferences)
        # that survives across separate chat sessions.
        ltm_context = _load_ltm_context(prompt, actor_id, account_name)

        invocation_id = str(uuid.uuid4())
        set_context(account_name, region, request_id=invocation_id)
        reset_tools_called(invocation_id)
        # Ordering: LTM facts first (broad context), then STM turns (recent context),
        # then the live prompt — this mirrors how humans recall general then specific context.
        enriched_prompt = ltm_context + stm_context + prompt

        async def event_generator():
            """Yield SSE-formatted events from agent.stream_async().

            SSE event type mapping:
              progress    — sent once at start to indicate routing has begun
              agent_switch — emitted when a tool call first appears, telling the
                             frontend which specialist is handling the request
              tool_call   — accompanies agent_switch with the raw tool name
              content     — one event per streamed text chunk from the model
              complete    — final event carrying the full assembled response
              error       — emitted on unrecoverable errors; generator returns after
            """
            import json as _json

            yield f"event: progress\ndata: {_json.dumps({'stage': 'routing', 'message': 'Routing to specialist agent'})}\n\n"

            agent_type = "supervisor"
            full_response = ""
            # Guard against duplicate agent_switch events: Strands fires current_tool_use
            # on every streaming delta while a tool is accumulating its input, so we
            # deduplicate by toolUseId rather than by event occurrence.
            emitted_tool_ids = set()

            # Maps Strands tool function names → frontend-facing specialist labels
            tool_map = {
                "check_cloudwatch": "cloudwatch",
                "check_security": "security",
                "analyze_costs": "cost",
                "check_advisor": "advisor",
                "manage_jira": "jira",
                "search_knowledge": "knowledge"
            }

            try:
                async for event in supervisor.stream_async(enriched_prompt):
                    logger.debug(f"Stream event keys: {list(event.keys()) if isinstance(event, dict) else type(event).__name__}")

                    if not isinstance(event, dict):
                        continue

                    # Tool use detection — fires on every input delta, emit agent_switch only once per tool
                    if "current_tool_use" in event:
                        tool_info = event["current_tool_use"]
                        tool_id = tool_info.get("toolUseId", "")
                        if tool_id and tool_id not in emitted_tool_ids:
                            emitted_tool_ids.add(tool_id)
                            tool_name = tool_info.get("name", "unknown")
                            agent_type = tool_map.get(tool_name, "supervisor")
                            yield f"event: agent_switch\ndata: {_json.dumps({'from_agent': 'supervisor', 'to_agent': agent_type, 'message': f'Delegating to {agent_type} specialist'})}\n\n"
                            yield f"event: tool_call\ndata: {_json.dumps({'tool_name': tool_name, 'agent': agent_type})}\n\n"

                    # Text streaming — plain dict with "data" key
                    elif "data" in event and event["data"]:
                        chunk = event["data"]
                        if isinstance(chunk, str):
                            full_response += chunk
                            yield f"event: content\ndata: {_json.dumps({'text': chunk, 'agent_type': agent_type, 'is_reasoning': agent_type == 'supervisor'})}\n\n"

                    # Final agent result — yielded after streaming loop completes
                    elif "result" in event:
                        try:
                            result = event["result"]
                            if hasattr(result, 'message') and result.message:
                                for block in result.message.get("content", []):
                                    if isinstance(block, dict) and "text" in block:
                                        text = block["text"]
                                        if text and not full_response.strip():
                                            full_response = text
                                            yield f"event: content\ndata: {_json.dumps({'text': text, 'agent_type': agent_type})}\n\n"
                        except Exception:  # nosec B110
                            pass

            except Exception as stream_err:
                err_str = str(stream_err)
                if "max_tokens" in err_str.lower() or "MaxTokensReached" in err_str or "max tokens" in err_str.lower():
                    logger.warning(f"MaxTokensReachedException caught in streaming: {stream_err}")
                    helpful_msg = (
                        "The response was too large to complete. Try a more specific query:\n"
                        "• Log groups: 'show log groups starting with /aws/lambda'\n"
                        "• Costs: 'top 5 services by cost last month'\n"
                        "• Security: 'show only CRITICAL findings'\n"
                        "• Alarms: 'show alarms in ALARM state only'"
                    )
                    full_response = helpful_msg
                    yield f"event: content\ndata: {_json.dumps({'text': helpful_msg, 'agent_type': agent_type})}\n\n"
                else:
                    logger.error(f"Streaming error: {stream_err}", exc_info=True)
                    yield f"event: error\ndata: {_json.dumps({'message': 'An error occurred processing your request'})}\n\n"
                    return

            # Fallback: if streaming yielded no text, extract from agent conversation state.
            # This occurs when Strands emits the final answer only inside the "result" event
            # but stream_async() completed without any "data" chunks (e.g. very short replies
            # or when the model returns the full turn in a single non-streamed block).
            # supervisor.messages holds the full Bedrock conversation history for this agent
            # instance; we walk it in reverse to find the most recent assistant turn.
            if not full_response.strip():
                logger.warning("No text captured from stream events — extracting from agent state")
                try:
                    if hasattr(supervisor, 'messages') and supervisor.messages:
                        for msg in reversed(supervisor.messages):
                            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
                            if role == 'assistant':
                                content_blocks = msg.get('content', []) if isinstance(msg, dict) else getattr(msg, 'content', [])
                                if isinstance(content_blocks, str):
                                    full_response = content_blocks
                                elif isinstance(content_blocks, list):
                                    parts = []
                                    for block in content_blocks:
                                        if isinstance(block, str):
                                            parts.append(block)
                                        elif isinstance(block, dict) and 'text' in block:
                                            parts.append(block['text'])
                                    full_response = "\n".join(p for p in parts if p)
                                if full_response.strip():
                                    logger.info(f"Extracted {len(full_response)} chars from agent conversation")
                                    yield f"event: content\ndata: {_json.dumps({'text': full_response, 'agent_type': agent_type})}\n\n"
                                    break
                except Exception as extract_err:
                    logger.warning(f"Failed to extract from agent messages: {extract_err}")

            effective_response = full_response
            logger.info(f"Stream done: full_response={len(full_response)}ch, effective={len(effective_response)}ch")
            _save_memory_turn(actor_id, session_id, prompt, effective_response)
            clear_tools_state(invocation_id)

            yield f"event: complete\ndata: {_json.dumps({'response': effective_response, 'agent_type': agent_type, 'account_name': account_name})}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )

    except Exception as e:
        logger.error(f"Error processing invocation: {e}", exc_info=True)
        async def error_stream():
            import json as _json
            yield f"event: error\ndata: {_json.dumps({'message': 'An error occurred processing your request'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")


@app.get("/ping")
def ping():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "agent": "supervisor",
        "protocol": "HTTP",
        "port": 8080
    }


if __name__ == "__main__":
    logger.info("Starting MSP Ops Supervisor Runtime on port 8080...")
    uvicorn.run(app, host="0.0.0.0", port=8080)  # nosec B104 -- intentional: containerized service must bind all interfaces for internal ALB/Gateway routing