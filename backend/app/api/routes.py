# app/api/routes.py
"""
API routes for MSP Assistant.

Handles chat, workflow, and user endpoints with an async fire-and-poll pattern
for long-running operations:
  1. POST /chat  — enqueues work in a background asyncio Task, returns request_id.
  2. GET  /chat/{id}     — client polls DynamoDB for the finished result.
  3. GET  /chat/{id}/stream — SSE stream that forwards progress events from DynamoDB
                              as the background Task writes them (see chat_state.py).

Workflow endpoints (/workflows/*) follow the same async pattern:
  step approval → background Task → DynamoDB → poll or SSE.

Auth endpoints (/auth/*) store Cognito refresh tokens in httpOnly cookies so
the frontend never has to touch localStorage for long-lived credentials.

Architecture note:
  All agent work runs in AgentCore Runtime (serverless).  This file is the
  HTTP surface only — no agent logic lives here.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Cookie, Response, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse
from typing import Dict, Optional
from pydantic import BaseModel, validator
from app.core.auth import get_current_user
from app.services.account_service import get_account_service
from app.core.secrets_credential_manager import get_current_msp_principal_arn, get_current_msp_account_id
from app.services.health_service import get_health_service
from app.services.workflow_service import get_workflow_service
from app.core.direct_router import get_direct_router
from app.core.config import settings
import asyncio
import threading
import uuid
import re
import logging
import os

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_error_message(exc: Exception) -> str:
    """Return a generic error message safe to surface to users.
    Full exception details are already logged at the call site before fail_request.
    """
    from botocore.exceptions import ClientError
    if isinstance(exc, ClientError):
        return f"AWS service error ({exc.response['Error']['Code']}). Check server logs for details."
    return "An unexpected error occurred. Please try again."

# AgentCore Memory client — shared across requests, lazy-initialized
_memory_client = None
_memory_client_lock = threading.Lock()

def _user_session_id(user_id: str) -> str:
    """Daily-rotating AgentCore Memory session ID — STM resets each day, LTM provides cross-day recall."""
    from datetime import date
    return f"msp-{user_id}-{date.today().isoformat()}"


def _get_memory_client():
    """Lazy-init AgentCore MemoryClient; returns None if MEMORY_ID not set."""
    global _memory_client
    if _memory_client is not None:
        return _memory_client
    memory_id = settings.MEMORY_ID
    if not memory_id:
        return None
    with _memory_client_lock:
        if _memory_client is not None:
            return _memory_client
        try:
            from bedrock_agentcore.memory import MemoryClient
            _memory_client = MemoryClient(region_name=settings.AWS_REGION)
            logger.info("AgentCore MemoryClient initialized for direct routing path")
        except Exception as e:
            logger.warning(f"AgentCore MemoryClient init failed: {e}")
    return _memory_client


# Cached SemanticFacts strategy ID — resolved once, reused across requests
_semantic_strategy_id = None
_semantic_strategy_id_lock = threading.Lock()


def _get_semantic_strategy_id() -> str:
    """Lazy-resolve the SemanticFacts memory strategy ID; returns '' if unavailable."""
    global _semantic_strategy_id
    if _semantic_strategy_id is not None:
        return _semantic_strategy_id
    memory_id = settings.MEMORY_ID
    if not memory_id:
        return ""
    with _semantic_strategy_id_lock:
        if _semantic_strategy_id is not None:
            return _semantic_strategy_id
        try:
            mem_client = _get_memory_client()
            if mem_client:
                strategies = mem_client.get_memory_strategies(memory_id)
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
    """Truncate a conversation turn for context — longer budget for assistant responses."""
    max_len = 200 if role.upper() == "USER" else 800
    if len(content) <= max_len:
        return content
    truncated = content[:max_len]
    last_period = truncated.rfind('. ')
    if last_period > max_len * 0.6:
        truncated = truncated[:last_period + 1]
    return truncated + " ... [truncated]"


# Shared regex for sanitizing account names — single source of truth for validator and helper
_ACCOUNT_NAME_UNSAFE_CHARS = r'[^a-z0-9_]'

# UUID format: 8-4-4-4-12 hex digits
_CONVERSATION_ID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


# Request/Response Models
class ChatRequest(BaseModel):
    message: str
    account_name: Optional[str] = "default"
    workflow_enabled: bool = False
    full_automation: bool = False
    conversation_id: Optional[str] = None

    @validator('account_name')
    @classmethod
    def sanitize_account_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Strip chars that could break the metadata JSON prefix injected into A2A prompts
        sanitized = re.sub(_ACCOUNT_NAME_UNSAFE_CHARS, '', v)
        if sanitized != v:
            logger.warning(f"account_name sanitized: {v!r} -> {sanitized!r}")
        return sanitized

    @validator('conversation_id')
    @classmethod
    def validate_conversation_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not _CONVERSATION_ID_PATTERN.match(v):
            raise ValueError("conversation_id must be a valid UUID")
        return v


def _sanitize_account_name(name: str) -> str:
    """Sanitize path-param account names (same rule as ChatRequest validator)."""
    return re.sub(_ACCOUNT_NAME_UNSAFE_CHARS, '', name)


class ChatResponse(BaseModel):
    success: bool
    content: str
    agent_type: str
    workflow_triggered: bool = False
    workflow_id: Optional[str] = None


class AccountCreateRequest(BaseModel):
    account_name: str
    account_id: str
    description: Optional[str] = None


class AccountResponse(BaseModel):
    id: str
    name: str
    account_id: str
    status: str
    role_name: str
    external_id: str
    created_at: Optional[str]
    needs_refresh: bool


# Helper Functions
# Keyword sets per domain — single source of truth for both routing functions
_AGENT_KEYWORDS = {
    'cost':       ['cost', 'spend', 'bill', 'budget', 'pricing', 'expense', 'saving'],
    'cloudwatch': ['alarm', 'cloudwatch', 'metric', 'log', 'monitor', 'cw', 'performance', 'cpu', 'memory', 'utilization'],
    'security':   ['security', 'finding', 'compliance', 'vulnerability', 'securityhub', 'risk'],
    'advisor':    ['advisor', 'best practice', 'recommendation', 'trusted', 'optimize', 'optimization', 'health check', 'health'],
    'jira':       ['jira', 'ticket', 'issue', 'incident'],
    'knowledge':  ['knowledge', 'troubleshoot', 'how to', 'guide', 'kb', 'fix', 'resolve'],
}
_COMPREHENSIVE_KEYWORDS = ['health check', 'complete', 'full', 'overview', 'summary', 'all', 'everything', 'environment', 'status']


def _detect_agent_stage(message: str) -> str:
    """Return the single best-match agent domain, or 'supervisor' for ambiguous queries."""
    return _detect_multi_agents(message)[0]


def _detect_multi_agents(message: str) -> list:
    """Return ordered list of agent domains needed to answer this query."""
    msg = message.lower()
    matches = {agent: any(w in msg for w in words) for agent, words in _AGENT_KEYWORDS.items()}
    is_comprehensive = any(w in msg for w in _COMPREHENSIVE_KEYWORDS)

    agents = []
    if is_comprehensive:
        if matches['cost'] or 'spending' in msg:
            agents.append('cost')
        agents.extend(['cloudwatch', 'security', 'advisor'])
    else:
        for agent in _AGENT_KEYWORDS:
            if matches[agent]:
                agents.append(agent)

    # Deduplicate while preserving order
    seen: set = set()
    unique = []
    for a in agents:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique if unique else ['supervisor']


def _detect_alarm_in_response(response_text: str) -> bool:
    """
    Regex-based heuristic that decides whether an agent response contains active alarms.

    First checks an explicit no-alarm phrase list to avoid false positives (e.g. a
    response that says "found 0 active alarms" would otherwise match the count regex).
    Then tries count-based patterns ("3 active alarms"), and finally scans for alarm
    state indicators such as emoji markers or status strings.

    Args:
        response_text: Raw text returned by a CloudWatch or other agent.

    Returns:
        True if the response appears to describe at least one active alarm.
    """
    text = response_text.lower()
    no_alarm_phrases = [
        "no active alarms", "0 active alarms", "no alarms",
        "found 0 active alarms", "not see any", "don't see any",
        "active alarms: 0", "zero alarms", "no active alarm"
    ]
    if any(phrase in text for phrase in no_alarm_phrases):
        return False
    # Regex patterns that extract the count of alarms from common agent phrasing.
    # If every matched count is 0 we return False; any count > 0 returns True.
    count_patterns = [
        r'(\d+)\s+active\s+alarms?',
        r'found\s+(\d+)\s+alarms?',
        r'(\d+)\s+alarms?\s+detected',
        r'(\d+)\s+alarms?\s+found',
    ]
    for pattern in count_patterns:
        count_matches = re.findall(pattern, text)
        if count_matches:
            for count in count_matches:
                if int(count) > 0:
                    return True
            return False
    alarm_indicators = [
        "alarm state", "status: alarm", "state: alarm",
        "🚨", "⚠️", "in alarm state",
        "currently have 1", "currently have 2",
        "[critical]", "active alarm", "critical alarm",
    ]
    return any(indicator in text for indicator in alarm_indicators)


async def _detect_remediation_intent_llm(response_text: str) -> bool:
    """
    Use Supervisor Runtime to classify whether a response contains
    actionable issues requiring remediation (alarms, security findings, cost anomalies).

    Falls back to regex on any failure.
    """
    try:
        SUPERVISOR_RUNTIME_ARN = os.getenv("SUPERVISOR_RUNTIME_ARN")
        AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
        if not SUPERVISOR_RUNTIME_ARN:
            return _detect_alarm_in_response(response_text)

        from app.core.agentcore_client import get_agentcore_client
        agentcore = get_agentcore_client(region=AWS_REGION)

        classification_prompt = f"""Classify this AWS agent response. Does it contain an actionable issue that needs remediation?

Response text (first 1500 chars):
{response_text[:1500]}

Actionable issues include:
- CloudWatch alarms in ALARM state
- Security Hub findings with CRITICAL or HIGH severity
- Cost anomalies or budget threshold breaches
- Trusted Advisor warnings requiring action

Reply with ONLY one word: YES or NO"""

        result = await agentcore.invoke_runtime(
            runtime_arn=SUPERVISOR_RUNTIME_ARN,
            payload={
                "prompt": classification_prompt,
                "session_id": f"intent-classify-{uuid.uuid4().hex[:8]}"
            }
        )
        answer = result.get("response", "").strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        logger.warning(f"LLM intent classification failed, falling back to regex: {e}")
        return _detect_alarm_in_response(response_text)


async def _should_trigger_remediation(response_text: str) -> bool:
    """
    Router: checks REMEDIATION_DETECTION_MODE config and delegates
    to LLM or regex detection accordingly.
    """
    from app.core.config_loader import REMEDIATION_DETECTION_MODE
    if REMEDIATION_DETECTION_MODE == "llm":
        return await _detect_remediation_intent_llm(response_text)
    return _detect_alarm_in_response(response_text)


def _build_routing_reason(message: str, agent_hint: str) -> str:
    """Build a human-readable explanation of why this agent was chosen."""
    msg = message.lower()
    keyword_map = {
        'cost':       ['cost', 'spend', 'bill', 'budget', 'pricing', 'expense', 'saving'],
        'cloudwatch': ['alarm', 'cloudwatch', 'metric', 'log', 'monitor', 'performance', 'cpu', 'memory'],
        'security':   ['security', 'finding', 'compliance', 'vulnerability', 'securityhub'],
        'advisor':    ['advisor', 'best practice', 'recommendation', 'trusted', 'optimize'],
        'jira':       ['jira', 'ticket', 'issue', 'incident'],
        'knowledge':  ['troubleshoot', 'how to', 'guide', 'kb', 'fix', 'resolve'],
    }
    if agent_hint in keyword_map:
        matched = [k for k in keyword_map[agent_hint] if k in msg]
        if matched:
            return f"Keywords detected: {', '.join(matched[:4])}"
    return f"{agent_hint} domain query"


async def _process_chat_async(request_id: str, request: ChatRequest, current_user: Dict):
    """
    Background Task that drives a single chat request end-to-end.

    This function is always invoked via asyncio.create_task() so POST /chat can
    return immediately.  All progress is written to DynamoDB via chat_state helpers
    and forwarded to connected SSE clients by get_progress_stream().

    High-level flow:
      1. Staggered asyncio Tasks emit routing/agent_switch/tool_call SSE events to
         DynamoDB so the frontend ThinkingDropdown has live progress before the
         AgentCore response arrives.
      2. Try direct routing: if the query maps to a single specialist domain, invoke
         that specialist's Runtime ARN directly (saves 45-90 s vs Supervisor roundtrip).
      3. Fallback to Supervisor Runtime streaming.  SSE events from the stream are
         forwarded atomically to DynamoDB (append_streaming_event + set_streaming_content).
      4. After the response arrives, check whether it describes active alarms/issues
         (via _should_trigger_remediation) and optionally start a workflow.
      5. Call complete_request() or fail_request() to unblock polling clients.

    Args:
        request_id: UUID that identifies this request in DynamoDB.
        request: Validated ChatRequest body.
        current_user: Decoded JWT claims dict from get_current_user().
    """
    import os
    from app.core.agentcore_client import get_agentcore_client
    from app.core.workspace_context import get_workspace_context
    from app.services.chat_state import (
        update_progress, complete_request, fail_request,
        append_streaming_event, set_streaming_content
    )

    agent_hint = _detect_agent_stage(request.message)
    agent_messages = {
        'cost':        'Querying Cost Explorer agent',
        'cloudwatch':  'Querying CloudWatch monitoring agent',
        'security':    'Scanning with Security Hub agent',
        'jira':        'Managing tickets with Jira agent',
        'advisor':     'Checking Trusted Advisor recommendations',
        'knowledge':   'Searching knowledge base',
        'supervisor':  'Processing with Supervisor agent',
    }
    tool_name_map = {
        'cost': 'analyze_costs', 'cloudwatch': 'check_cloudwatch',
        'security': 'check_security', 'advisor': 'check_advisor',
        'jira': 'manage_jira', 'knowledge': 'search_knowledge',
        'supervisor': 'supervisor',
    }

    try:
        SUPERVISOR_RUNTIME_ARN = os.getenv("SUPERVISOR_RUNTIME_ARN")
        AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

        if not SUPERVISOR_RUNTIME_ARN:
            raise Exception("SUPERVISOR_RUNTIME_ARN not configured")

        user_id = current_user.get("user_id", current_user.get("sub"))
        # Per-conversation session ID ties STM (short-term memory) to this browser tab's
        # conversation.  If the frontend sends a conversation_id the session is stable;
        # without it we fall back to a daily-rotating session so at least same-day
        # turns are grouped.  LTM always persists via actor_id regardless.
        if not request.conversation_id:
            logger.warning(f"No conversation_id in chat request from user {user_id} — falling back to daily session")
        session_id = f"msp-{user_id}-{request.conversation_id}" if request.conversation_id else _user_session_id(user_id)

        workspace = get_workspace_context()
        if request.account_name and request.account_name != "default":
            workspace.set_current_account(request.account_name)
        else:
            workspace.clear_context()

        account_name = request.account_name or "default"

        # Build rich routing reasoning for ThinkingDropdown
        query_snippet = request.message[:80] + ('...' if len(request.message) > 80 else '')
        routing_reason = _build_routing_reason(request.message, agent_hint)
        expected_agents = _detect_multi_agents(request.message)
        specialist_tool = tool_name_map.get(agent_hint, agent_hint)

        # Steps 1-4 below fire staggered SSE progress events to DynamoDB so that the
        # frontend ThinkingDropdown shows live activity instead of a blank spinner
        # while the AgentCore Runtime call is in flight.  The tasks are cancelled
        # immediately if a response arrives before the timer fires (avoids duplicate
        # events on fast responses).

        # Emit step 1: Supervisor analyzing (immediate)
        append_streaming_event(request_id, {
            "event": "progress",
            "data": {"stage": "routing", "message": f"Supervisor analyzing: \"{query_snippet}\""}
        })

        # Emit step 2: Routing decision with reason (after 1s)
        async def _emit_routing():
            await asyncio.sleep(1)
            update_progress(request_id, "routing", "Routing to specialist agent")
            append_streaming_event(request_id, {
                "event": "agent_switch",
                "data": {
                    "from_agent": "supervisor",
                    "to_agent": agent_hint,
                    "message": f"Routing to {agent_hint} specialist",
                    "routing_reason": routing_reason,
                    "agent_index": 1,
                    "agent_count": len(expected_agents) if len(expected_agents) > 1 else 1,
                }
            })
            append_streaming_event(request_id, {
                "event": "tool_call",
                "data": {
                    "tool_name": specialist_tool,
                    "agent": agent_hint,
                    "routing_reason": query_snippet,
                }
            })

        # Emit step 3: Execution progress (after 3s)
        async def _emit_delegating():
            await asyncio.sleep(3)
            exec_msg = agent_messages.get(agent_hint, f"{agent_hint} agent processing")
            update_progress(request_id, "delegating", exec_msg)
            append_streaming_event(request_id, {
                "event": "progress",
                "data": {"stage": "delegating", "message": exec_msg}
            })

        # Emit step 4: Waiting (after 15s)
        async def _emit_waiting():
            await asyncio.sleep(15)
            update_progress(request_id, "waiting", "Agent is analyzing data and generating response")
            append_streaming_event(request_id, {
                "event": "progress",
                "data": {"stage": "waiting", "message": "Agent is analyzing data and generating response"}
            })

        _progress_tasks = [
            asyncio.create_task(_emit_routing()),
            asyncio.create_task(_emit_delegating()),
            asyncio.create_task(_emit_waiting()),
        ]

        # For multi-domain queries, emit a separate agent_switch event per specialist
        # so the ThinkingDropdown shows each agent being invoked in sequence.
        # Delays are staggered (5 s, 17 s, 29 s, …) to roughly match real agent latency.
        if len(expected_agents) > 1:
            for idx, agent_key in enumerate(expected_agents):
                delay = 5 + idx * 12
                agent_msg = agent_messages.get(agent_key, f"Querying {agent_key} agent")
                labeled_msg = agent_msg.replace("...", f"... ({idx + 1}/{len(expected_agents)})")

                async def _fire_multi_agent(msg=labeled_msg, d=delay, ak=agent_key, i=idx):
                    await asyncio.sleep(d)
                    update_progress(request_id, "delegating", msg)
                    if i > 0:
                        prev_agent = expected_agents[i - 1]
                        append_streaming_event(request_id, {
                            "event": "agent_switch",
                            "data": {
                                "from_agent": prev_agent,
                                "to_agent": ak,
                                "message": f"Querying {ak} specialist ({i + 1}/{len(expected_agents)})",
                                "routing_reason": f"Multi-domain query requires {ak} data",
                                "agent_index": i + 1,
                                "agent_count": len(expected_agents),
                            }
                        })

                _progress_tasks.append(asyncio.create_task(_fire_multi_agent()))

        async def _complete_with_workflow(resp: str, atype: str) -> None:
            """Shared finalization for both direct-routing and Supervisor paths."""
            wf_triggered = False
            wf_id = None
            alarm_detected = await _should_trigger_remediation(resp)
            logger.info(f"Workflow check [{request_id}]: enabled={request.workflow_enabled}, agent={atype!r}, alarm_detected={alarm_detected}")
            # In LLM mode, allow security/cost/advisor responses to trigger workflow too
            from app.core.config_loader import REMEDIATION_DETECTION_MODE
            agent_qualifies = (
                "cloudwatch" in atype.lower()
                or (REMEDIATION_DETECTION_MODE == "llm" and atype.lower() in ("security", "cost", "advisor", "supervisor"))
            )
            if request.workflow_enabled and agent_qualifies and alarm_detected:
                try:
                    workflow_service = get_workflow_service()
                    wr = await workflow_service.start_workflow(
                        request.message,
                        request.account_name,
                        full_automation=request.full_automation,
                        has_alarm=True,
                        cloudwatch_response=resp,
                        user_id=user_id,
                        session_id=session_id
                    )
                    logger.info(f"Workflow start result [{request_id}]: {wr}")
                    if wr.get("success") and wr.get("requires_approval"):
                        wf_id = wr["workflow_id"]
                        wf_triggered = True
                        if request.full_automation:
                            resp += "\n\n---\n\n**Full Automation Mode Active**\n\nRemediation steps will be executed automatically."
                except Exception as e:
                    logger.warning(f"Workflow start failed [{request_id}]: {e}")
            complete_request(request_id, {
                "success": True,
                "content": resp,
                "agent_type": atype,
                "workflow_triggered": wf_triggered,
                "workflow_id": wf_id,
            })

        # --- Load conversation history from AgentCore Memory (direct routing path) ---
        # The Supervisor Runtime loads memory internally (supervisor_runtime.py).
        # For the direct-routing path we load it here and prepend it to the prompt so
        # specialists receive the same recent-turns context they would get via Supervisor.
        memory_context_prefix = ""
        memory_id = settings.MEMORY_ID
        if memory_id:
            try:
                mem_client = _get_memory_client()
                if mem_client:
                    turns = mem_client.get_last_k_turns(
                        memory_id=memory_id, actor_id=user_id, session_id=session_id, k=3
                    )
                    if turns:
                        lines = []
                        for turn in turns:
                            for msg in turn:
                                role = msg.get('role', '')
                                content = msg.get('content', {}).get('text', '')
                                content = _truncate_for_context(content, role)
                                lines.append(f"{role}: {content}")
                        memory_context_prefix = "Previous conversation:\n" + "\n".join(lines) + "\n\n"
            except Exception as mem_err:
                logger.warning(f"Failed to load memory context: {mem_err}")

        # --- LTM (Long-Term Memory) semantic retrieval ---
        # SemanticFacts records are keyed by namespace = /strategy/{id}/actor/{user}/account/{acct}/
        # so facts are automatically scoped: different users or accounts never bleed into
        # each other's context even though they share the same memory resource.
        ltm_context = ""
        ltm_strategy_id = _get_semantic_strategy_id()
        if ltm_strategy_id and settings.MEMORY_ID:
            try:
                mem_client = _get_memory_client()
                if mem_client:
                    namespace = f"/strategy/{ltm_strategy_id}/actor/{user_id}/account/{account_name}/"
                    records = mem_client.retrieve_memories(
                        memory_id=settings.MEMORY_ID,
                        namespace=namespace,
                        query=request.message,
                        top_k=5,
                    )
                    if records:
                        facts = [r.get("content", {}).get("text", "") for r in records]
                        facts = [f for f in facts if f]
                        if facts:
                            ltm_context = "Relevant context from previous conversations:\n" + "\n".join(f"- {f}" for f in facts[:5]) + "\n\n"
                            logger.info(f"LTM retrieval: {len(facts)} facts for {request_id}")
            except Exception as ltm_err:
                logger.warning(f"LTM retrieval failed (non-fatal): {ltm_err}")

        # --- Direct routing: bypass Supervisor for unambiguous single-domain queries ---
        # Saves 45-90 s by calling the specialist A2A Runtime ARN directly, skipping
        # the Supervisor's tool-selection roundtrip.  Credential injection is identical
        # to the Supervisor path: account_name is embedded in a metadata JSON prefix that
        # context_tools._extract_metadata_prompt() reads on the specialist side.
        # Falls back to Supervisor if the specialist ARN is not configured or returns None.
        if agent_hint != 'supervisor':
            try:
                direct_router = get_direct_router()
                if direct_router.can_route_directly(agent_hint):
                    logger.info(f"Direct routing to {agent_hint} for {request_id}")
                    enriched_prompt = ltm_context + memory_context_prefix + request.message
                    direct_result = await direct_router.invoke_specialist(
                        agent_key=agent_hint,
                        prompt=enriched_prompt,
                        account_name=account_name,
                        region=AWS_REGION,
                        session_id=session_id,
                    )
                    if direct_result is not None:
                        response = direct_result["response"]
                        agent_type = direct_result["agent_type"]

                        if response:
                            set_streaming_content(request_id, response)
                            response_preview = response[:150] + ('...' if len(response) > 150 else '')
                            append_streaming_event(request_id, {
                                "event": "tool_call",
                                "data": {
                                    "tool_name": "result",
                                    "agent": agent_type,
                                    "status": "complete",
                                    "result_preview": response_preview,
                                }
                            })

                        # Save turn to memory so Supervisor queries can reference it later
                        if memory_id and response:
                            try:
                                mem_client = _get_memory_client()
                                if mem_client:
                                    mem_client.create_event(
                                        memory_id=memory_id,
                                        actor_id=user_id,
                                        session_id=session_id,
                                        messages=[(request.message, "USER"), (response, "ASSISTANT")]
                                    )
                            except Exception as mem_save_err:
                                logger.warning(f"Failed to save direct-route turn to memory: {mem_save_err}")

                        for t in _progress_tasks:
                            t.cancel()
                        await _complete_with_workflow(response, agent_type)
                        return
                    else:
                        logger.info(f"DirectRouter returned None for {agent_hint}, falling back to Supervisor")
            except Exception as dr_err:
                logger.warning(f"Direct routing failed for {agent_hint}, falling back to Supervisor: {dr_err}")
        # --- End direct routing ---

        # Cancel staggered SSE progress tasks — the Supervisor streaming path emits its
        # own agent_switch/tool_call events, so the pre-emptive timers would duplicate them.
        for t in _progress_tasks:
            t.cancel()

        # Invoke Supervisor via streaming; fall back to non-streaming on error
        update_progress(request_id, "delegating", agent_messages.get(agent_hint, "Processing with Supervisor agent"))
        agentcore = get_agentcore_client(region=AWS_REGION)

        payload = {
            "prompt": request.message,
            "account_name": account_name,
            "workflow_enabled": request.workflow_enabled,
            "full_automation": request.full_automation,
            "session_id": session_id,
            "user_context": {
                "user_id": user_id,
                "email": current_user.get("email"),
                "account_name": account_name
            },
        }

        response = ""
        agent_type = "supervisor"
        streaming_succeeded = False
        content_chunk_count = 0

        try:
            # Stream SSE events from the Supervisor Runtime and forward each one to
            # DynamoDB so the polling SSE endpoint (get_progress_stream) can relay
            # them to the browser in near real-time.
            async for evt in agentcore.invoke_runtime_stream(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload=payload,
                session_id=session_id,
            ):
                event_name = evt.get("event", "")
                event_data = evt.get("data", {})

                if event_name == "error":
                    logger.warning(f"Streaming error for {request_id}: {event_data.get('message')}")
                    break

                if event_name in ("agent_switch", "tool_call", "progress"):
                    append_streaming_event(request_id, evt)
                    if event_name == "agent_switch":
                        to_agent = event_data.get("to_agent", "")
                        if to_agent:
                            agent_type = to_agent
                            update_progress(request_id, "delegating",
                                            agent_messages.get(to_agent, f"{to_agent} agent processing"))
                elif event_name == "content":
                    chunk = event_data.get("text", "")
                    if chunk:
                        response += chunk
                        content_chunk_count += 1
                        set_streaming_content(request_id, response)
                        if event_data.get("agent_type"):
                            agent_type = event_data["agent_type"]
                        # Persist reasoning flag so SSE poller can forward it
                        if event_data.get("is_reasoning"):
                            append_streaming_event(request_id, {
                                "event": "content_meta",
                                "data": {"is_reasoning": True, "agent_type": event_data.get("agent_type", "")}
                            })
                elif event_name == "complete":
                    complete_response = event_data.get("response", response)
                    complete_agent = event_data.get("agent_type", agent_type)
                    if complete_response:
                        response = complete_response
                    if complete_agent:
                        agent_type = complete_agent
                    streaming_succeeded = True
                    break

            if response:
                set_streaming_content(request_id, response)

        except Exception as stream_err:
            logger.warning(f"Streaming invoke failed for {request_id}: {stream_err}")

        # Streaming failed (connection error, cold-start timeout, etc.) — retry with a
        # single blocking invoke_runtime() call so the request still completes.
        if not streaming_succeeded:
            logger.info(f"Falling back to non-streaming invoke for {request_id}")
            result = await agentcore.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload=payload,
                session_id=session_id,
            )
            response = result.get("response", "")
            agent_type = result.get("agent_type", "supervisor")

        await _complete_with_workflow(response, agent_type)
        
    except Exception as e:
        logger.error(f"Error processing chat {request_id}: {e}", exc_info=True)
        fail_request(request_id, _safe_error_message(e))


async def _process_workflow_automation_async(request_id: str, workflow_id: str, current_user: Dict):
    """Background processor for full workflow automation."""
    from app.services.chat_state import update_progress, complete_request, fail_request
    from app.services.workflow_service import get_workflow_service
    
    try:
        update_progress(request_id, "routing", "Starting full automation workflow")
        await asyncio.sleep(2)
        update_progress(request_id, "delegating", "Step 1/4: Creating Jira ticket")
        await asyncio.sleep(1)
        workflow_service = get_workflow_service()
        result = await workflow_service.execute_full_automation(workflow_id, use_dynamic=True)
        complete_request(request_id, {
            "success": result.get("success", False),
            "content": result.get("message", "Automation completed"),
            "agent_type": "workflow_automation",
            "workflow_complete": True,
            "step_results": result.get("result", {}).get("steps", [])
        })
    except Exception as e:
        logger.error(f"Error in workflow automation {request_id}: {e}", exc_info=True)
        fail_request(request_id, _safe_error_message(e))


async def _process_workflow_step_async(request_id: str, workflow_id: str, step_type: str, current_user: Dict):
    """Background processor for individual workflow step approvals."""
    from app.services.chat_state import update_progress, complete_request, fail_request, append_streaming_event
    from app.services.workflow_service import get_workflow_service

    step_messages = {
        "jira": "Creating Jira ticket",
        "kb_search": "Searching knowledge base",
        "remediation": "Executing remediation",
        "closure": "Closing Jira ticket",
        "full_auto": "Running full automation",
    }

    try:
        update_progress(request_id, "executing", step_messages.get(step_type, "Processing workflow step..."))
        workflow_service = get_workflow_service()

        if step_type == "full_auto":
            # Track per-step status to detect mutations
            # (workflow_graph mutates existing dicts from "executing" -> "completed")
            step_statuses = {}  # {step_num: last_seen_status}

            def on_step_progress(results):
                steps = results.get("steps", [])
                for i, step in enumerate(steps):
                    step_num = step.get("step_num", i + 1)
                    current_status = step.get("status", "")
                    prev_status = step_statuses.get(step_num)

                    # Emit if new step (prev_status is None) or status changed
                    if current_status != prev_status:
                        step_statuses[step_num] = current_status
                        append_streaming_event(request_id, {
                            "event": "progress",
                            "data": {
                                "type": "workflow_step",
                                "step_num": step_num,
                                "step_name": step.get("step") or step.get("message", ""),
                                "status": current_status,
                                "result": (step.get("result", "") or "")[:2000] if current_status == "completed" else "",
                                "message": step.get("message", ""),
                            }
                        })

            result = await workflow_service.approve_step(workflow_id, step_type, progress_callback=on_step_progress)
        else:
            result = await workflow_service.approve_step(workflow_id, step_type)

        complete_request(request_id, result)
    except Exception as e:
        logger.error(f"Error in workflow step {step_type} for {request_id}: {e}", exc_info=True)
        fail_request(request_id, _safe_error_message(e))


# Auth token endpoints — no JWT required (these establish the session)
_REFRESH_COOKIE = "msp_refresh_token"
_COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days, matches Cognito refresh_token_validity


class SetRefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/set-refresh")
async def auth_set_refresh(body: SetRefreshRequest, response: Response):
    """
    Store the Cognito refresh token in an httpOnly cookie.
    Called once after sign-in so the refresh token never lives in localStorage.
    """
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=body.refresh_token,
        httponly=True,
        secure=True,       # HTTPS only
        samesite="none",   # cross-site: cloudfront.net → amazonaws.com
        max_age=_COOKIE_MAX_AGE,
        path="/",
    )
    return {"success": True}


@router.post("/auth/restore")
async def auth_restore(request: Request):
    """
    Restore a session from the httpOnly refresh cookie.
    Called on page load to re-hydrate in-memory tokens without touching localStorage.
    Returns idToken + accessToken in the response body (not cookies).
    """
    import boto3 as _boto3
    refresh_token = request.cookies.get(_REFRESH_COOKIE)
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token cookie")

    try:
        cognito = _boto3.client("cognito-idp", region_name=settings.AWS_REGION)
        result = cognito.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": refresh_token},
            ClientId=settings.COGNITO_CLIENT_ID,
        )
        auth_result = result["AuthenticationResult"]
        id_token = auth_result["IdToken"]
        access_token = auth_result["AccessToken"]

        # Decode user info from the IdToken without verifying (already trusted via Cognito)
        import base64, json as _json
        payload_b64 = id_token.split(".")[1]
        # Add padding if needed
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = _json.loads(base64.b64decode(payload_b64))

        response = JSONResponse({
            "success": True,
            "idToken": id_token,
            "accessToken": access_token,
            "user": {
                "userId": payload.get("sub"),
                "email": payload.get("email") or payload.get("cognito:username"),
                "username": payload.get("cognito:username") or payload.get("email"),
            },
        })

        # Rotate the cookie if Cognito issued a new refresh token
        new_refresh = auth_result.get("RefreshToken")
        if new_refresh:
            response.set_cookie(
                key=_REFRESH_COOKIE, value=new_refresh,
                httponly=True, secure=True, samesite="none",
                max_age=_COOKIE_MAX_AGE, path="/",
            )
        return response

    except Exception as e:
        err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        logger.warning(f"Session restore failed: {e}")
        http_status = status.HTTP_401_UNAUTHORIZED if err_code in ("NotAuthorizedException", "InvalidParameterException") else status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=http_status, detail="Session restore failed")


@router.post("/auth/logout")
async def auth_logout(response: Response):
    """Clear the httpOnly refresh token cookie on sign-out."""
    response.delete_cookie(key=_REFRESH_COOKIE, path="/", httponly=True, secure=True, samesite="none")
    return {"success": True}


@router.get("/chat/history")
async def get_chat_history(
    k: int = 10,
    conversation_id: Optional[str] = None,
    current_user: Dict = Depends(get_current_user),
):
    """Return the last k conversation turns from AgentCore Memory for the current user."""
    k = min(k, 50)  # Cap to prevent unbounded memory fetches

    # Validate conversation_id format (same rule as ChatRequest validator)
    if conversation_id is not None:
        conversation_id = conversation_id.strip().lower()
        if not _CONVERSATION_ID_PATTERN.match(conversation_id):
            raise HTTPException(status_code=400, detail="conversation_id must be a valid UUID")

    memory_id = settings.MEMORY_ID
    if not memory_id:
        return {"success": True, "messages": []}

    user_id = current_user.get("user_id", current_user.get("sub"))
    session_id = f"msp-{user_id}-{conversation_id}" if conversation_id else _user_session_id(user_id)

    try:
        mem_client = _get_memory_client()
        if not mem_client:
            return {"success": True, "messages": []}

        turns = mem_client.get_last_k_turns(
            memory_id=memory_id, actor_id=user_id, session_id=session_id, k=k
        )
        if not turns:
            return {"success": True, "messages": []}

        messages = []
        for turn in turns:
            for msg in turn:
                role = msg.get("role", "")
                content = msg.get("content", {}).get("text", "")
                if not content:
                    continue
                sender = "user" if role.upper() == "USER" else "agent"
                messages.append({
                    "id": str(uuid.uuid4()),
                    "sender": sender,
                    "content": content,
                    "agentType": None,  # Original agent type not stored in memory
                    "timestamp": msg.get("timestamp", ""),
                })
        return {"success": True, "messages": messages}
    except Exception as e:
        logger.warning(f"Failed to load chat history: {e}")
        return {"success": True, "messages": []}


# Routes
@router.get("/me")
async def get_user_info(current_user: Dict = Depends(get_current_user)):
    """
    GET /me — return the authenticated user's JWT claims.

    Auth: Bearer JWT (Cognito).
    Response: {user: {user_id, email, …}, authenticated: true}
    """
    return {"user": current_user, "authenticated": True, "message": "Token is valid"}


@router.get("/msp-principal")
async def get_msp_principal(current_user: Dict = Depends(get_current_user)):
    """
    GET /msp-principal — return the MSP ECS task IAM principal ARN and account ID.

    Auth: Bearer JWT (Cognito).
    Response: {success, principal_arn, account_id}
    Used by the frontend to display which MSP identity is making cross-account calls.
    """
    try:
        return {
            "success": True,
            "principal_arn": get_current_msp_principal_arn(),
            "account_id": get_current_msp_account_id()
        }
    except Exception as e:
        logger.warning(f"MSP principal lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to get MSP principal")


@router.post("/chat")
async def send_chat_message(request: ChatRequest, current_user: Dict = Depends(get_current_user)):
    """
    POST /chat — submit a chat message for async processing.

    Auth: Bearer JWT (Cognito).
    Request body: ChatRequest {message, account_name, workflow_enabled, full_automation, conversation_id}
    Response: {request_id, status: "processing"}

    Immediately returns a request_id and spawns _process_chat_async as a background Task.
    Clients poll GET /chat/{request_id} or stream GET /chat/{request_id}/stream.
    """
    from app.services.chat_state import create_request
    request_id = str(uuid.uuid4())
    user_id = current_user.get("user_id", current_user.get("sub"))
    agent_hint = _detect_agent_stage(request.message)
    create_request(request_id, user_id, agent_hint)
    asyncio.create_task(_process_chat_async(request_id, request, current_user))
    return {"request_id": request_id, "status": "processing"}


@router.get("/chat/{request_id}")
async def get_chat_result(request_id: str, current_user: Dict = Depends(get_current_user)):
    """
    GET /chat/{request_id} — poll once for a chat request's current state.

    Auth: Bearer JWT (Cognito).
    Response: {request_id, status, progress, result, streaming_events}
    Status values: "processing" | "complete" | "error"
    Returns 404 if the request_id is not found or belongs to a different user.
    """
    from app.services.chat_state import get_request
    user_id = current_user.get("user_id", current_user.get("sub"))
    entry = get_request(request_id, user_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found or access denied")
    return {
        "request_id": request_id,
        "status": entry["status"],
        "progress": entry["progress"],
        "result": entry.get("result"),
        "streaming_events": entry.get("streaming_events", []),
    }


@router.get("/chat/{request_id}/stream")
async def stream_chat_result(request_id: str, current_user: Dict = Depends(get_current_user)):
    """
    GET /chat/{request_id}/stream — Server-Sent Events stream for a chat request.

    Auth: Bearer JWT (Cognito).
    Media type: text/event-stream

    Delegates to chat_state.get_progress_stream() which polls DynamoDB at 300 ms
    intervals and yields SSE-formatted strings.  Event types forwarded:
      progress, agent_switch, tool_call, content, complete, error.

    X-Accel-Buffering: no disables nginx proxy buffering so chunks reach the browser
    immediately.  API Gateway has a 29 s idle timeout; a heartbeat comment is emitted
    every 20 s to prevent the connection from being closed prematurely.
    """
    from app.services.chat_state import get_progress_stream
    user_id = current_user.get("user_id", current_user.get("sub"))
    return StreamingResponse(
        get_progress_stream(request_id, user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/health/protected")
async def protected_health_check(current_user: Dict = Depends(get_current_user)):
    return {"status": "healthy", "authenticated": True, "user_id": current_user["user_id"], "email": current_user["email"], "message": "Protected endpoint is working"}


@router.get("/accounts")
async def list_accounts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: Dict = Depends(get_current_user),
):
    """
    GET /accounts — paginated list of registered customer accounts.

    Auth: Bearer JWT (Cognito).
    Query params: page (default 1), page_size (1-100, default 50)
    Response: {success, accounts: [...], total, page, page_size}

    A synthetic "Default (Current MSP)" entry is always prepended so the frontend
    can select the MSP's own account without special-casing.
    """
    try:
        account_service = get_account_service()
        result = await account_service.list_accounts()
        if result["success"]:
            accounts = [{"id": "default", "name": "Default (Current MSP)", "type": "msp", "status": "active"}]
            for account in result["accounts"]:
                accounts.append({
                    "id": account["id"], "name": account["name"],
                    "account_id": account["account_id"], "type": "customer",
                    "status": account["status"], "role_name": account["role_name"],
                    "external_id": account["external_id"],
                    "created_at": account.get("created_at"), "needs_refresh": account["needs_refresh"]
                })
            total = len(accounts)
            start = (page - 1) * page_size
            page_accounts = accounts[start: start + page_size]
            return {"success": True, "accounts": page_accounts, "total": total, "page": page, "page_size": page_size}
        else:
            return {"success": True, "accounts": [{"id": "default", "name": "Default (Current MSP)", "type": "msp", "status": "active"}], "total": 1, "page": 1, "page_size": page_size, "warning": result.get("message")}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to list accounts")


@router.post("/accounts/prepare")
async def prepare_account(request: Dict, current_user: Dict = Depends(get_current_user)):
    """
    POST /accounts/prepare — validate cross-account IAM access before adding an account.

    Auth: Bearer JWT (Cognito).
    Request body: {account_name: str}
    Response: {success, message, ...}

    Performs an STS AssumeRole dry-run to confirm the MSP role trust policy is correct
    before the account is stored.
    """
    try:
        account_name = request.get("account_name")
        if not account_name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_name is required")
        account_service = get_account_service()
        result = await account_service.prepare_account(account_name)
        if result["success"]:
            return result
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("message", "Preparation failed"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Account preparation failed")


@router.post("/accounts")
async def create_account(request: AccountCreateRequest, current_user: Dict = Depends(get_current_user)):
    """
    POST /accounts — register a new customer AWS account.

    Auth: Bearer JWT (Cognito).
    Request body: AccountCreateRequest {account_name, account_id, description?}
    Response 200: {success, message, account}
    Response 400: if account already exists or IAM role not accessible.
    """
    try:
        account_service = get_account_service()
        result = await account_service.create_account(request.account_name, request.account_id, request.description)
        if result["success"]:
            return {"success": True, "message": result["message"], "account": result["account"]}
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Account creation failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Account creation failed")


@router.delete("/accounts/{account_name}")
async def delete_account(account_name: str, current_user: Dict = Depends(get_current_user)):
    """
    DELETE /accounts/{account_name} — remove a customer account registration.

    Auth: Bearer JWT (Cognito).
    Path param: account_name (sanitized — unsafe chars stripped).
    Response 200: {success, message}
    Response 400: if account not found.
    """
    account_name = _sanitize_account_name(account_name)
    try:
        account_service = get_account_service()
        result = await account_service.delete_account(account_name)
        if result.get("success"):
            return {"success": True, "message": result["message"]}
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("message", "Unknown error"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Account deletion failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Account deletion failed")


@router.put("/accounts/{account_name}/refresh")
async def refresh_account(account_name: str, current_user: Dict = Depends(get_current_user)):
    """
    PUT /accounts/{account_name}/refresh — refresh the STS credentials for one account.

    Auth: Bearer JWT (Cognito).
    Path param: account_name (sanitized).
    Response: {success, message, expires_at?}
    """
    account_name = _sanitize_account_name(account_name)
    try:
        account_service = get_account_service()
        result = await account_service.refresh_account(account_name)
        if result["success"]:
            return result
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Token refresh failed")


@router.post("/accounts/refresh-all")
async def refresh_all_accounts(current_user: Dict = Depends(get_current_user)):
    """
    POST /accounts/refresh-all — re-issue STS credentials for every registered account.

    Auth: Bearer JWT (Cognito).
    Response: {success, refreshed, failed, accounts}
    Useful to pre-warm credentials before a bulk health check.
    """
    try:
        account_service = get_account_service()
        return await account_service.refresh_all_accounts()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Refresh all failed")


@router.post("/accounts/{account_name}/switch")
async def switch_account(account_name: str, current_user: Dict = Depends(get_current_user)):
    """
    POST /accounts/{account_name}/switch — set the active account context for this process.

    Auth: Bearer JWT (Cognito).
    Path param: account_name; pass "default" to revert to the MSP account.
    Response: {success, account_name, session?}
    Note: this mutates in-process workspace state; it is safe because the ECS task
    handles one request at a time per asyncio event loop.
    """
    account_name = _sanitize_account_name(account_name)
    try:
        account_service = get_account_service()
        return await account_service.switch_account_context(account_name if account_name != "default" else None)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Account switch failed")


@router.get("/health/summary")
async def get_health_summary(current_user: Dict = Depends(get_current_user)):
    """
    GET /health/summary — AWS Service Health Dashboard summary for active events.

    Auth: Bearer JWT (Cognito).
    Response: {success, events, summary}
    """
    try:
        health_service = get_health_service()
        return await health_service.get_health_summary()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Health summary failed")


@router.get("/health/outages")
async def get_health_outages(current_user: Dict = Depends(get_current_user)):
    """
    GET /health/outages — current AWS regional outages from the Health API.

    Auth: Bearer JWT (Cognito).
    Response: {success, outages: [...]}
    """
    try:
        health_service = get_health_service()
        return await health_service.get_outages()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch outages")


@router.get("/health/scheduled")
async def get_health_scheduled(current_user: Dict = Depends(get_current_user)):
    """
    GET /health/scheduled — upcoming AWS scheduled maintenance windows.

    Auth: Bearer JWT (Cognito).
    Response: {success, maintenance: [...]}
    """
    try:
        health_service = get_health_service()
        return await health_service.get_scheduled_maintenance()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch scheduled maintenance")


@router.get("/health/notifications")
async def get_health_notifications(current_user: Dict = Depends(get_current_user)):
    """
    GET /health/notifications — recent AWS Health event notifications.

    Auth: Bearer JWT (Cognito).
    Response: {success, notifications: [...]}
    """
    try:
        health_service = get_health_service()
        return await health_service.get_notifications()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch notifications")


@router.get("/workflows/pending")
async def get_pending_workflows(current_user: Dict = Depends(get_current_user)):
    """
    GET /workflows/pending — list workflows waiting for human approval.

    Auth: Bearer JWT (Cognito).
    Response: {success, approvals: [{workflow_id, type, query, ...}]}
    Scans DynamoDB for items whose request_id begins with "approval-".
    """
    try:
        workflow_service = get_workflow_service()
        return await workflow_service.get_pending_approvals()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch pending workflows")


@router.post("/workflows/{workflow_id}/approve/{step_type}")
async def approve_workflow_step(workflow_id: str, step_type: str, current_user: Dict = Depends(get_current_user)):
    """
    POST /workflows/{workflow_id}/approve/{step_type} — approve and execute one workflow step.

    Auth: Bearer JWT (Cognito).
    Path params:
      workflow_id — UUID of the workflow.
      step_type   — one of: jira | kb_search | remediation | verification | closure | full_auto
    Response: {success, request_id, status: "processing"}

    Follows the same async fire-and-poll pattern as /chat: work runs in a background
    Task (_process_workflow_step_async) and the client polls via GET /chat/{request_id}.
    """
    try:
        from app.services.chat_state import create_request
        request_id = str(uuid.uuid4())
        user_id = current_user.get("user_id", current_user.get("sub"))
        create_request(request_id, user_id, f"workflow_{step_type}")
        asyncio.create_task(_process_workflow_step_async(request_id, workflow_id, step_type, current_user))
        return {"success": True, "request_id": request_id, "status": "processing"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Step approval failed")


@router.post("/workflows/{workflow_id}/reject/{step_type}")
async def reject_workflow_step(workflow_id: str, step_type: str, current_user: Dict = Depends(get_current_user)):
    """
    POST /workflows/{workflow_id}/reject/{step_type} — reject a pending workflow step.

    Auth: Bearer JWT (Cognito).
    Path params: workflow_id, step_type (same values as approve endpoint).
    Response: {success, message}
    Deletes the "approval-{workflow_id}" DynamoDB item, ending the workflow.
    """
    try:
        workflow_service = get_workflow_service()
        return await workflow_service.reject_step(workflow_id, step_type)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Step rejection failed")


@router.get("/workflows/{workflow_id}/status")
async def get_workflow_status(workflow_id: str, current_user: Dict = Depends(get_current_user)):
    """
    GET /workflows/{workflow_id}/status — current state of a workflow.

    Auth: Bearer JWT (Cognito).
    Response: {success, workflow_id, created_at, has_alarm, pending_approval}
    Returns 404 if the workflow_id is unknown.
    """
    try:
        workflow_service = get_workflow_service()
        result = await workflow_service.get_workflow_status(workflow_id)
        if result.get("success"):
            return result
        else:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.get("message"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Status check failed")


@router.post("/workflows/{workflow_id}/automate")
async def execute_full_automation(workflow_id: str, current_user: Dict = Depends(get_current_user)):
    """
    POST /workflows/{workflow_id}/automate — kick off full end-to-end automation.

    Auth: Bearer JWT (Cognito).
    Response: {success, request_id, status: "processing"}

    Runs all 5 steps (Jira + KB in parallel, then Remediation → Verification → Closure)
    in a background Task.  Clients poll GET /chat/{request_id} for completion.
    """
    try:
        from app.services.chat_state import create_request
        request_id = str(uuid.uuid4())
        user_id = current_user.get("user_id", current_user.get("sub"))
        create_request(request_id, user_id, "workflow_automation")
        asyncio.create_task(_process_workflow_automation_async(request_id, workflow_id, current_user))
        return {"success": True, "request_id": request_id, "status": "processing"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Automation failed")


@router.get("/workflows/{workflow_id}/progress")
async def get_automation_progress(workflow_id: str, current_user: Dict = Depends(get_current_user)):
    """
    GET /workflows/{workflow_id}/progress — per-step automation progress.

    Auth: Bearer JWT (Cognito).
    Note: Detailed progress tracking is not yet persisted to DynamoDB.
    This endpoint currently returns success: False with a descriptive message.
    Use GET /chat/{request_id}/stream for real-time step events instead.
    """
    try:
        workflow_service = get_workflow_service()
        return await workflow_service.get_automation_progress(workflow_id)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Progress check failed")
