"""
DynamoDB-backed async chat state store.

Supports multi-container ECS deployments: because asyncio Tasks run inside a single
ECS container but the polling/streaming client may hit a different container on the
next request, all state is persisted to DynamoDB so every container sees the same
data.

Key design decisions:
  - TTL_SECONDS (1 hour) provides generous buffer for Security Hub scans (~130 s)
    and multi-step workflow executions without storing stale records indefinitely.
  - streaming_events is a DynamoDB list that _process_chat_async appends to
    atomically as each SSE event arrives from the Supervisor Runtime.
    get_progress_stream() reads new events by tracking last_events_count and slicing.
  - streaming_content is a single string field overwritten on each content chunk.
    The SSE poller forwards only the incremental delta (streaming_content[last_content_len:])
    so the client receives a character stream rather than repeatedly receiving the full
    accumulated response.
  - Heartbeat comments (": heartbeat") are emitted every 20 s to prevent API Gateway's
    29 s idle connection timeout from closing an in-progress SSE stream.
"""
import boto3
import os
import time
import logging
from typing import Optional, Dict, Any
from decimal import Decimal

logger = logging.getLogger(__name__)

TABLE_NAME = os.getenv("CHAT_REQUESTS_TABLE", "msp-assistant-chat-requests")
TTL_SECONDS = 3600  # 1 hour — generous buffer for slow agents (Security Hub can take 130s)

_dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
_table = _dynamodb.Table(TABLE_NAME)

_ALLOWED_SSE_EVENT_TYPES = frozenset({"progress", "agent_switch", "tool_call", "content", "content_meta", "complete", "error"})


def _decimal_to_float(obj):
    """Convert DynamoDB Decimal types to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


def create_request(request_id: str, user_id: str, agent_hint: str) -> Dict:
    """
    Create a new chat request entry in DynamoDB with "processing" status.

    Called immediately before spawning the background Task so the polling
    endpoint has something to return on the very first poll.

    Args:
        request_id: UUID that uniquely identifies this request.
        user_id: Cognito sub/user_id — used for ownership check in get_request().
        agent_hint: Best-guess agent domain from _detect_agent_stage() (e.g. "cloudwatch").
                    Stored in progress so the frontend can display a routing indicator
                    before the first streaming event arrives.

    Returns:
        The created item dict with Decimal types converted to float.
    """
    now = time.time()
    item = {
        "request_id": request_id,
        "user_id": user_id,
        "status": "processing",
        "progress": {
            "stage": "received",
            "agent_hint": agent_hint,
            "message": "Analyzing your request...",
            "started_at": Decimal(str(now)),
            "elapsed_seconds": Decimal("0"),
        },
        "result": None,
        "created_at": int(now),
        "ttl": int(now + TTL_SECONDS),
    }
    _table.put_item(Item=item)
    return _decimal_to_float(item)


def update_progress(request_id: str, stage: str, message: str):
    """Update progress stage (called during background processing)."""
    try:
        _table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET progress.stage = :s, progress.#msg = :m",
            ExpressionAttributeNames={"#msg": "message"},
            ExpressionAttributeValues={":s": stage, ":m": message},
        )
    except Exception as e:
        # Don't fail the entire request if progress update fails
        logger.warning(f"Failed to update progress for {request_id}: {e}")


def append_streaming_event(request_id: str, event: Dict[str, Any]) -> None:
    """
    Append a single SSE event dict to the DynamoDB streaming_events list.
    Called from _process_chat_async as each SSE event arrives from the Supervisor.
    Uses DynamoDB list_append to atomically grow the event list.
    """
    try:
        _table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET streaming_events = list_append(if_not_exists(streaming_events, :empty), :evt)",
            ExpressionAttributeValues={
                ":evt": [event],
                ":empty": [],
            },
        )
    except Exception as e:
        logger.warning(f"Failed to append streaming event for {request_id}: {e}")


def set_streaming_content(request_id: str, content: str) -> None:
    """
    Overwrite the streaming_content field with accumulated response text.
    Called periodically from _process_chat_async as content chunks arrive.
    """
    try:
        _table.update_item(
            Key={"request_id": request_id},
            UpdateExpression="SET streaming_content = :c",
            ExpressionAttributeValues={":c": content},
        )
    except Exception as e:
        logger.warning(f"Failed to set streaming content for {request_id}: {e}")


def complete_request(request_id: str, result: Dict):
    """
    Mark a request as complete and store the final result in DynamoDB.

    Once this is written, get_progress_stream() will emit a "complete" SSE event
    carrying the full result dict and return, and GET /chat/{id} will return
    status="complete" with the result payload.

    Args:
        request_id: UUID of the request to finalize.
        result: Final result dict (must be JSON-serializable; no Decimal types).
                Expected keys: success, content, agent_type, workflow_triggered?,
                workflow_id?, step_results?, etc.
    """
    now = time.time()
    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression="SET #s = :status, #r = :result, progress.stage = :stage, progress.#msg = :msg, progress.elapsed_seconds = :elapsed",
        ExpressionAttributeNames={"#s": "status", "#r": "result", "#msg": "message"},
        ExpressionAttributeValues={
            ":status": "complete",
            ":result": result,
            ":stage": "complete",
            ":msg": "Response ready",
            ":elapsed": Decimal(str(now)),
        },
    )


def fail_request(request_id: str, error_message: str):
    """
    Mark a request as failed and store a safe error message in DynamoDB.

    The message should be user-safe (no stack traces, no AWS account IDs).
    Use _safe_error_message() in routes.py to sanitize exceptions before passing
    them here.  Full exception details should already be logged at the call site.

    Args:
        request_id: UUID of the request to fail.
        error_message: Human-readable error string to surface to the frontend.
    """
    _table.update_item(
        Key={"request_id": request_id},
        UpdateExpression="SET #s = :status, #r = :result, progress.stage = :stage, progress.#msg = :msg",
        ExpressionAttributeNames={"#s": "status", "#r": "result", "#msg": "message"},
        ExpressionAttributeValues={
            ":status": "error",
            ":result": {
                "success": False,
                "content": error_message,
                "agent_type": "error",
            },
            ":stage": "error",
            ":msg": error_message,
        },
    )


async def get_progress_stream(request_id: str, user_id: str):
    """
    Async generator that polls DynamoDB and yields SSE-formatted strings.

    Enhanced to forward streaming_events written by _process_chat_async as
    each SSE event arrives from the Supervisor Runtime. This enables real-time
    thinking/reasoning display in the frontend.

    Yields SSE events until the request is complete or errored.
    Used by GET /chat/{request_id}/stream endpoint.

    Args:
        request_id: The request ID to stream progress for
        user_id: User ID for security check

    Yields:
        SSE-formatted strings (e.g., "event: progress\\ndata: {...}\\n\\n")
    """
    import asyncio
    import json as _json

    MAX_WAIT_SECONDS = 300  # 5 minutes max
    POLL_INTERVAL = 0.3     # Poll every 300ms for smoother streaming
    HEARTBEAT_INTERVAL = 20.0  # SSE heartbeat cadence to prevent API Gateway 29s idle timeout
    elapsed = 0.0
    last_stage = None
    last_events_count = 0   # Track how many streaming_events we've already forwarded
    last_content_len = 0    # Track how many content chars we've already forwarded
    last_heartbeat = 0.0    # Track when we last sent an SSE heartbeat

    while elapsed < MAX_WAIT_SECONDS:
        # SSE comment heartbeat — keeps the HTTP/1.1 connection alive through API Gateway's
        # 29 s idle timeout.  The 20 s cadence provides a comfortable safety margin.
        # Browser SSE clients silently discard comment lines (lines starting with ": ").
        if elapsed - last_heartbeat >= HEARTBEAT_INTERVAL:
            yield ": heartbeat\n\n"
            last_heartbeat = elapsed

        entry = get_request(request_id, user_id)

        if not entry:
            yield f"event: error\ndata: {_json.dumps({'message': 'Request not found or access denied'})}\n\n"
            return

        status = entry.get("status", "processing")
        progress = entry.get("progress", {})
        stage = progress.get("stage", "")
        message = progress.get("message", "")

        # Forward any NEW streaming_events appended since our last poll.
        # last_events_count tracks how many we've already yielded so we only
        # forward the slice [last_events_count:] on each iteration.
        streaming_events = entry.get("streaming_events", [])
        if len(streaming_events) > last_events_count:
            new_events = streaming_events[last_events_count:]
            for evt in new_events:
                event_type = evt.get("event", "progress")
                event_data = evt.get("data", {})
                # Allowlist guards against SSE header injection if a DynamoDB item
                # were tampered with to include an arbitrary event type string.
                if event_type not in _ALLOWED_SSE_EVENT_TYPES:
                    logger.warning(f"Dropping unknown SSE event type: {event_type!r}")
                    continue
                # content chunks are forwarded separately via the streaming_content delta
                # path below to avoid double-delivery.  content_meta events carry flags
                # (e.g. is_reasoning) consumed internally to annotate the next delta.
                if event_type not in ("content", "content_meta"):
                    yield f"event: {event_type}\ndata: {_json.dumps(event_data)}\n\n"
            last_events_count = len(streaming_events)

        # Forward only the NEW characters from streaming_content on each poll cycle.
        # The background Task overwrites the full accumulated string on every chunk;
        # we send only streaming_content[last_content_len:] so the client receives
        # a progressive character stream without re-receiving earlier characters.
        streaming_content = entry.get("streaming_content", "")
        if streaming_content and len(streaming_content) > last_content_len:
            delta = streaming_content[last_content_len:]
            last_content_len = len(streaming_content)
            # Determine current agent from last agent_switch event
            current_agent = "supervisor"
            for evt in reversed(streaming_events):
                if evt.get("event") == "agent_switch":
                    current_agent = evt.get("data", {}).get("to_agent", "supervisor")
                    break
            # Determine reasoning flag from content_meta events
            is_reasoning = False
            for evt in reversed(streaming_events):
                if evt.get("event") == "content_meta":
                    is_reasoning = evt.get("data", {}).get("is_reasoning", False)
                    break
            yield f"event: content\ndata: {_json.dumps({'text': delta, 'agent_type': current_agent, 'is_reasoning': is_reasoning})}\n\n"

        # Yield progress stage update if changed — but ONLY when no streaming_events
        # exist yet (initial "Analyzing..." phase). Once streaming_events start flowing,
        # they carry all progress/agent_switch/tool_call info and the parallel stage
        # updates from update_progress() would create duplicates.
        if stage != last_stage:
            last_stage = stage
            if not streaming_events:
                yield f"event: progress\ndata: {_json.dumps({'stage': stage, 'message': message})}\n\n"

        # Check terminal states
        if status == "complete":
            result = entry.get("result", {})
            agent_type = result.get("agent_type", "unknown")

            # Yield complete event with full result
            yield f"event: complete\ndata: {_json.dumps({'result': result, 'agent_type': agent_type})}\n\n"
            return

        elif status == "error":
            result = entry.get("result", {})
            error_msg = result.get("content", "Unknown error")
            yield f"event: error\ndata: {_json.dumps({'message': error_msg})}\n\n"
            return

        # Still processing — wait and poll again
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    # Timeout
    yield f"event: error\ndata: {_json.dumps({'message': 'Request timed out after 5 minutes'})}\n\n"


def get_request(request_id: str, user_id: str) -> Optional[Dict]:
    """
    Get request state from DynamoDB.
    Returns None if not found or wrong user (security check).
    """
    try:
        resp = _table.get_item(Key={"request_id": request_id})
        item = resp.get("Item")
        
        if not item:
            return None
        
        # Security: Only return if user_id matches
        if item.get("user_id") != user_id:
            return None
        
        # Convert Decimal types to float for JSON
        item = _decimal_to_float(item)
        
        # Compute elapsed time for processing requests
        if item["status"] == "processing" and "progress" in item:
            started_at = item["progress"].get("started_at", time.time())
            item["progress"]["elapsed_seconds"] = time.time() - started_at
        
        return item
        
    except Exception as e:
        logger.error(f"Error getting request {request_id}: {e}")
        return None
