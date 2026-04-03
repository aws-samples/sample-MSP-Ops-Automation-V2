"""
Workflow Graph - AgentCore Runtime Integration

This module handles workflow orchestration by invoking AgentCore Runtime agents.

Architecture:
- Backend is orchestration layer ONLY (no agent logic)
- Agents run in AgentCore Runtime (serverless microservices)
- Backend invokes agents via boto3.invoke_agent_runtime()
- All state stored in DynamoDB (multi-container safe)

Remediation Pattern (from FE-BE-decoupling):
  KB retrieval → Supervisor generates XML plan → Guard validates → boto3 executes

  Phase 1: Supervisor Runtime produces an XML plan:
           <step>aws service action --param value</step>
  Phase 2: _parse_xml_steps() extracts CLI commands (plus optional on_success/on_failure
           conditional attributes) without executing anything.
  Phase 2.5: Deterministic guard_policies.validate_steps() rejects blocklisted operations
              instantly, before the LLM guard is even consulted.
  Phase 3: Supervisor Runtime validates the full step list against KB context.
  Phase 4: Approved steps are executed one-by-one via Gateway HTTP → aws-api-mcp MCP target.

Workflow Steps (sequential, each requires prior human approval in step-by-step mode):
  1. approve_workflow       — create Jira ticket
  2. approve_kb_search      — search Bedrock Knowledge Base
  3. approve_remediation    — generate + guard-validate + execute CLI plan
  4. approve_verification   — poll CloudWatch alarm state with retry loop
  5. close_jira_ticket      — update and close the Jira ticket

See architecture diagram for full flow:
  Users → CloudFront → API GW → ALB → ECS → AgentCore Runtime
"""
from typing import Dict, Any, Optional, List
import logging
import uuid
import os
import re
import json
import asyncio
import boto3
from app.core.agentcore_client import AgentCoreClient, get_agentcore_client
from app.core.config_loader import JIRA_PROJECT_KEY, AWS_REGION, GUARD_MODE, VERIFICATION_MAX_RETRIES, VERIFICATION_RETRY_DELAY_SECONDS
from app.services.kb_retriever import BedrockKBRetriever
from app.services.resource_id_resolver import ResourceIDResolver
from app.services.guard_policies import validate_steps

logging.basicConfig(
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# AgentCore Runtime configuration
SUPERVISOR_RUNTIME_ARN = os.getenv("SUPERVISOR_RUNTIME_ARN", "")
AWS_API_MCP_ARN = os.getenv("AWS_API_MCP_ARN", "")
GATEWAY_URL = os.getenv("GATEWAY_URL", "")


class CloudWatchJiraKBRemediationGraph:
    """
    Workflow Orchestrator - Invokes AgentCore Runtime for agent execution
    
    Architecture:
    - This class does NOT run agents in-process
    - Agents run in AgentCore Runtime (serverless)
    - This class invokes Runtime via boto3.invoke_agent_runtime()
    - All state stored in DynamoDB (no in-memory state between steps)
    
    Workflow Steps:
    1. CloudWatch analysis → AgentCore Runtime
    2. Jira ticket creation → AgentCore Runtime  
    3. Knowledge base search → Direct Bedrock KB retrieval
    4. Remediation execution → KB-driven with guard validation
    5. Ticket closure → AgentCore Runtime
    """
    
    def __init__(self, customer_session=None):
        self.customer_session = customer_session
        self._last_workflow_result = None
        
        # AgentCore Runtime is required - no in-process fallback
        if not SUPERVISOR_RUNTIME_ARN:
            logger.warning("SUPERVISOR_RUNTIME_ARN not set - workflows require AgentCore Runtime")
        else:
            logger.info(f"Workflow orchestrator initialized - Runtime: {SUPERVISOR_RUNTIME_ARN[:50]}...")
    
    def start_workflow(self, query: str):
        """
        Start a new workflow by invoking AgentCore Runtime
        
        Returns:
            tuple: (workflow_id, result) where result contains alarm detection status
        """
        workflow_id = str(uuid.uuid4())
        
        if not SUPERVISOR_RUNTIME_ARN:
            logger.warning("Cannot start workflow - SUPERVISOR_RUNTIME_ARN not configured")
            result = type('Result', (), {
                'task_id': workflow_id,
                'has_alarm': False,
                'results': {},
                'message': "AgentCore Runtime not configured"
            })()
            self._last_workflow_result = result
            return workflow_id, result
        
        # Workflow start happens in routes.py (CloudWatch analysis)
        # This method just returns workflow_id for tracking
        result = type('Result', (), {
            'task_id': workflow_id,
            'has_alarm': False,
            'results': {},
            'message': "Workflow started - CloudWatch analysis in routes.py"
        })()
        
        self._last_workflow_result = result
        return workflow_id, result
    
    def _extract_cloudwatch_metadata(self, text: str) -> dict:
        """
        Extract structured metadata from CloudWatch alarm text.

        Extracts metric_name, namespace, service, dimensions, threshold,
        state_value, and alarm_name from CloudWatch response text.
        """
        metadata = {}

        # Alarm name
        alarm_name = self._extract_alarm_name(text)
        if alarm_name:
            metadata["alarm_name"] = alarm_name

        # MetricName
        m = re.search(r'MetricName[:\s]+[\'"]?(\S+)', text, re.IGNORECASE)
        if m:
            metadata["metric_name"] = m.group(1).strip("'\"")

        # Namespace
        m = re.search(r'Namespace[:\s]+[\'"]?(\S+)', text, re.IGNORECASE)
        if m:
            metadata["namespace"] = m.group(1).strip("'\"")

        # Derive service from namespace
        namespace_service_map = {
            "AWS/ApiGateway": "apigateway",
            "AWS/EC2": "ec2",
            "AWS/ECS": "ecs",
            "AWS/RDS": "rds",
            "AWS/Lambda": "lambda",
            "AWS/ELB": "elb",
            "AWS/ALB": "elbv2",
            "AWS/S3": "s3",
            "AWS/DynamoDB": "dynamodb",
            "AWS/SQS": "sqs",
            "AWS/SNS": "sns",
            "AWS/CloudFront": "cloudfront",
            "AWS/ElastiCache": "elasticache",
        }
        ns = metadata.get("namespace", "")
        metadata["service"] = namespace_service_map.get(ns, "")

        # If no namespace match, try to infer service from alarm name
        if not metadata["service"] and alarm_name:
            alarm_lower = alarm_name.lower()
            for svc in ["apigateway", "ec2", "ecs", "rds", "lambda", "elb", "s3", "dynamodb", "sqs"]:
                if svc in alarm_lower:
                    metadata["service"] = svc
                    break

        # Dimensions (key-value pairs)
        dims = {}
        for dm in re.finditer(r'(\w+):\s*([a-zA-Z0-9_/-]+)', text):
            key, val = dm.group(1), dm.group(2)
            if key.lower() in ("name", "value") or len(val) > 80:
                continue
            dims[key] = val
        metadata["dimensions"] = dims

        # Threshold
        m = re.search(r'[Tt]hreshold[:\s]+([0-9.]+)', text)
        if m:
            metadata["threshold"] = m.group(1)

        # StateValue
        m = re.search(r'StateValue[:\s]+[\'"]?(\w+)', text, re.IGNORECASE)
        if m:
            metadata["state_value"] = m.group(1)

        logger.info(f"[METADATA] Extracted CloudWatch metadata: {metadata}")
        return metadata

    def _extract_alarm_name(self, text: str) -> Optional[str]:
        """
        Extract alarm name from CloudWatch response text.

        Patterns are ordered from most reliable (backtick-wrapped LLM output) to
        least specific (ARN fragment).  The first match wins.  Full alarm names
        including suffixes like "-alarm" or "-critical" are preserved; earlier
        versions stripped these suffixes which caused lookup failures.

        Args:
            text: Raw CloudWatch agent response or user query text.

        Returns:
            Alarm name string, or None if no pattern matched and TEST_ALARM_NAME
            env var is not set.
        """
        # Ordered regex patterns — capture full names, don't strip suffixes
        patterns = [
            # 1. Backtick-wrapped (most reliable — LLM output format)
            r'`([a-zA-Z0-9][a-zA-Z0-9_-]+)`',

            # 2. AlarmName field (from CloudWatch describe-alarms JSON/text output)
            r'AlarmName[:\s]+[`"\']?([a-zA-Z0-9][a-zA-Z0-9_-]+)[`"\']?',

            # 3. "alarm:" or "Alarm:" followed by the name
            r'[Aa]larm[:\s]+[`"\']?([a-zA-Z0-9][a-zA-Z0-9_-]+)[`"\']?',

            # 4. ARN fragment: …:alarm/name or …:alarm:name
            r'alarm[:/]([a-zA-Z0-9_-]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                alarm_name = match.group(1)
                logger.info(f"[EXTRACT_ALARM] Extracted alarm name: {alarm_name}")
                return alarm_name
        
        # Fallback to config-based test alarm
        test_alarm = os.getenv('TEST_ALARM_NAME')
        if test_alarm:
            logger.info(f"[EXTRACT_ALARM] No alarm name found in text, using TEST_ALARM_NAME: {test_alarm}")
            return test_alarm
        
        logger.warning("[EXTRACT_ALARM] Could not extract alarm name from CloudWatch response")
        return None
    
    def _parse_xml_steps(self, response_text: str) -> List[Dict]:
        """
        Parse XML remediation steps from the Supervisor's plan response.

        The Supervisor is prompted to emit one <step> tag per AWS CLI command.
        Optional conditional attributes allow branching logic:
          <step on_success="N">aws ...</step>  — only run if step N succeeded
          <step on_failure="N">aws ...</step>  — only run if step N failed

        The regex anchors on "aws " to avoid capturing non-CLI content that may
        accidentally appear inside <step> tags.

        Args:
            response_text: Raw text from the Supervisor Runtime plan invocation.

        Returns:
            List of dicts, each with keys:
              cli_command (str), on_success (int|None), on_failure (int|None)
        """
        steps = []
        # Regex captures optional on_success/on_failure group 1/2, then the CLI command (group 3).
        # re.DOTALL handles multi-line commands (e.g., --patch-operations spanning multiple lines).
        for match in re.finditer(
            r'<step(?:\s+on_success="(\d+)")?(?:\s+on_failure="(\d+)")?\s*>(aws\s+.+?)</step>',
            response_text, re.DOTALL
        ):
            on_success = int(match.group(1)) if match.group(1) else None
            on_failure = int(match.group(2)) if match.group(2) else None
            cli_command = match.group(3).strip()
            steps.append({
                "cli_command": cli_command,
                "on_success": on_success,
                "on_failure": on_failure,
            })
        return steps
    
    async def _call_gateway_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call MCP tool on Gateway via HTTP POST with SigV4 auth.
        
        Per AWS docs: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html
        
        Args:
            tool_name: Tool name in format "{TargetName}___{ToolName}" (e.g., "aws-api-mcp___call_aws")
            arguments: Tool arguments dict
            
        Returns:
            Dict with success, output, error
        """
        import httpx
        from botocore.session import Session as BotocoreSession
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from urllib.parse import urlparse
        
        if not GATEWAY_URL:
            return {"success": False, "error": "GATEWAY_URL not configured"}
        
        # Build MCP JSON-RPC request (per AWS docs)
        request_id = str(uuid.uuid4())
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments
            }
        })
        
        # SigV4 sign (same pattern as gateway_client.py)
        parsed = urlparse(GATEWAY_URL)
        host = parsed.netloc
        
        creds = BotocoreSession().get_credentials().get_frozen_credentials()
        headers = {
            "Content-Type": "application/json",
            "host": host
        }
        
        aws_req = AWSRequest(
            method="POST",
            url=GATEWAY_URL,
            data=body,
            headers=headers
        )
        SigV4Auth(creds, "bedrock-agentcore", AWS_REGION).add_auth(aws_req)
        
        # Execute HTTP request
        try:
            async with httpx.AsyncClient(timeout=420.0) as client:  # 7 min timeout for MCP cold-start
                response = await client.post(
                    GATEWAY_URL,
                    content=body,
                    headers=dict(aws_req.headers)
                )
                response.raise_for_status()
                
                result = response.json()
                
                # Parse MCP JSON-RPC response
                if "error" in result:
                    return {
                        "success": False,
                        "error": result["error"].get("message", "Unknown MCP error"),
                        "error_code": result["error"].get("code")
                    }
                
                # Check for tool execution errors (MCP spec: isError flag in result)
                mcp_result = result.get("result", {})
                if isinstance(mcp_result, dict) and mcp_result.get("isError", False):
                    # Extract error message from content
                    content = mcp_result.get("content", [])
                    error_text = content[0].get("text", "Tool execution failed") if content else "Tool execution failed"
                    return {
                        "success": False,
                        "error": error_text[:500]  # Truncate long errors
                    }
                
                return {
                    "success": True,
                    "output": mcp_result
                }
                
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def _boto3_step_to_cli(self, step: Dict) -> str:
        """
        Convert boto3-style step dict to AWS CLI command string.
        
        Args:
            step: Dict with 'service', 'operation', 'parameters'
            
        Returns:
            AWS CLI command string
            
        Example:
            Input:  {'service': 'apigateway', 'operation': 'update_rest_api', 
                     'parameters': {'restApiId': 'abc', 'patchOperations': [...]}}
            Output: 'aws apigateway update-rest-api --rest-api-id abc --patch-operations [...]'
        """
        service = step['service']
        
        # Convert Python snake_case operation to kebab-case CLI subcommand
        operation = step['operation'].replace('_', '-')
        
        # Build CLI command
        cli_parts = ['aws', service, operation]
        
        # Convert parameters to CLI flags
        for key, value in step['parameters'].items():
            # Convert camelCase to kebab-case
            flag = '--' + re.sub(r'([A-Z])', r'-\1', key).lower().lstrip('-')
            
            # Handle different value types
            if isinstance(value, (dict, list)):
                # JSON-encode complex values with compact formatting (no spaces)
                # AWS CLI parser expects valid JSON without shell quoting
                json_value = json.dumps(value, separators=(',', ':'))
                cli_parts.extend([flag, json_value])
            elif isinstance(value, bool):
                # Boolean flags
                if value:
                    cli_parts.append(flag)
            else:
                # Simple string/number values
                cli_parts.extend([flag, str(value)])
        
        return ' '.join(cli_parts)
    
    async def approve_workflow(
        self,
        workflow_id: str,
        query: str,
        account_name: str,
        step_results: Dict[str, Any],
        cloudwatch_response: str = "",
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Step 1: Create a Jira ticket for the detected alarm via AgentCore Runtime.

        Builds a structured ticket description from the CloudWatch response text and
        invokes the Supervisor Runtime with an imperative prompt that forces it to
        call manage_jira immediately (without asking for clarification).  The prompt
        uses command-style language ("EXECUTE THIS COMMAND IMMEDIATELY") because
        conversational phrasing causes the Supervisor to ask for the project key.

        After creation, REST API issue URLs (/rest/api/3/issue/N) are rewritten to
        the human-friendly browse URL (/browse/PROJ-123) for display in the frontend.

        Args:
            workflow_id: UUID of the current workflow (used for session scoping).
            query: Original user query about the alarm.
            account_name: Customer AWS account name for cross-account context.
            step_results: Results accumulated from prior steps (empty at step 1).
            cloudwatch_response: Full CloudWatch agent response containing alarm details.
                                 Falls back to query if empty.
            session_id: AgentCore Memory session ID for STM continuity.

        Returns:
            Dict with keys: success (bool), response (str), agent_type (str).
        """
        if not SUPERVISOR_RUNTIME_ARN:
            return {
                "success": False,
                "response": "Jira ticket creation requires AgentCore Runtime - SUPERVISOR_RUNTIME_ARN not configured",
                "agent_type": "error"
            }

        try:
            logger.info(f"Step 1: Creating Jira ticket for workflow {workflow_id}")
            
            # Use singleton client (avoids recreating boto3 clients per step)
            agentcore = get_agentcore_client(region=AWS_REGION)
            
            # Extract alarm name from CloudWatch response
            alarm_name = self._extract_alarm_name(cloudwatch_response or query)
            
            # Build ticket description
            if alarm_name:
                summary = f"CloudWatch Alarm: {alarm_name}"
                description = f"""Automated ticket from CloudWatch alarm detection.

**Alarm:** {alarm_name}

**Original Query:** {query}

**CloudWatch Analysis:**
{cloudwatch_response[:800]}

**Next Steps:**
1. Review alarm details in CloudWatch console
2. Check KB search results (Step 2)
3. Execute remediation (Step 3)
4. Verify alarm returns to OK state"""
            else:
                # Fallback if alarm name not extracted
                summary = "CloudWatch Alarm Investigation"
                description = f"""Automated ticket from CloudWatch alarm detection.

**Original Query:** {query}

**CloudWatch Response:**
{cloudwatch_response[:1000] if cloudwatch_response else query}

**Next Steps:**
1. Review CloudWatch alarms
2. Check KB search results (Step 2)
3. Execute remediation (Step 3)"""
            
            # Build prompt that routes to Jira agent via Supervisor
            # CRITICAL: Must explicitly command tool usage for Supervisor to route correctly
            # CRITICAL: Use imperative command format to prevent conversational response
            prompt = f"""EXECUTE THIS COMMAND IMMEDIATELY:

Call the manage_jira tool with these EXACT parameters:
- project_key: "{JIRA_PROJECT_KEY}"
- summary: "{summary}"
- description: "{description}"
- issue_type: "Task"
- labels: ["cloudwatch", "alarm", "automated"]

DO NOT ask for clarification. DO NOT ask for the project key. The project key is "{JIRA_PROJECT_KEY}".

Execute the tool call NOW and return the ticket key and URL."""
            
            # Invoke Supervisor Runtime (will delegate to Jira A2A agent)
            # Pass project_key explicitly in payload for Supervisor to use
            result = await agentcore.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": prompt,
                    "account_name": account_name,
                    "session_id": session_id or f"workflow-{workflow_id}-jira",
                    "jira_project_key": JIRA_PROJECT_KEY  # Explicit parameter for Supervisor
                }
            )
            
            response = result.get("response", "")
            agent_type = result.get("agent_type", "jira")
            
            # Post-process Jira URLs: Convert REST API format to browse format
            # Pattern: https://domain.atlassian.net/rest/api/3/issue/12345
            # Target:  https://domain.atlassian.net/browse/PROJ-123
            jira_domain = os.getenv("JIRA_DOMAIN", "").rstrip("/")
            if jira_domain and "/rest/api/" in response:
                # Try to extract ticket key from response (e.g., "MD-123")
                ticket_key_match = re.search(r'\b([A-Z]+-\d+)\b', response)
                if ticket_key_match:
                    ticket_key = ticket_key_match.group(1)
                    browse_url = f"{jira_domain}/browse/{ticket_key}"
                    # Replace any REST API URLs with browse URL
                    response = re.sub(
                        r'https?://[^/]+/rest/api/\d+/issue/\d+',
                        browse_url,
                        response
                    )
                    logger.info(f"Converted Jira URL to browse format: {browse_url}")
            
            logger.info("Step 1 complete: Jira ticket created")
            
            return {
                "success": True,
                "response": response,
                "agent_type": agent_type
            }
            
        except Exception as e:
            logger.error(f"Step 1 failed: {e}")
            return {
                "success": False,
                "response": f"Jira ticket creation failed: {str(e)}",
                "agent_type": "error"
            }
    
    def reject_workflow(self, workflow_id: str):
        """Reject workflow"""
        return f"Workflow {workflow_id} rejected"
    
    async def approve_kb_search(
        self,
        workflow_id: str,
        query: str,
        account_name: str,
        step_results: Dict[str, Any],
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Step 2: Search the Bedrock Knowledge Base for remediation guidance.

        Extracts structured metadata (service, metric, namespace, alarm name) from
        the CloudWatch response text, then uses BedrockKBRetriever.retrieve_with_fallback()
        to try three progressively broader query strategies until results are found.

        As a bonus step (alarm correlation), the method also queries CloudWatch for
        other alarms currently in ALARM state within the same namespace.  Related alarm
        names are appended to the KB query to improve root-cause matching (e.g. an API
        Gateway 5xx alarm that co-occurs with a Lambda throttle alarm).

        Raw KB content XML tags (<step>, <rollback>) are stripped before display so
        the frontend shows clean prose, but the full raw kb_results list is returned
        for Step 3 (remediation plan generation).

        Args:
            workflow_id: UUID of the current workflow.
            query: CloudWatch agent response text (NOT the raw user query).
            account_name: Customer AWS account name.
            step_results: Accumulated results (contains jira result from Step 1).
            session_id: AgentCore Memory session ID.

        Returns:
            Dict with keys: success, response (str), agent_type, kb_results (list),
            kb_confidence (str), related_alarms (list).
        """
        try:
            logger.info(f"Step 2: Searching Bedrock Knowledge Base for workflow {workflow_id}")
            
            # Extract structured metadata from CloudWatch output
            metadata = self._extract_cloudwatch_metadata(query)
            alarm_name = metadata.get("alarm_name") or self._extract_alarm_name(query)

            # Step 7: Alarm correlation — find related active alarms for better root-cause analysis
            related_alarms = []
            try:
                if GATEWAY_URL and metadata.get("namespace"):
                    logger.info("[KB_SEARCH] Checking for related active alarms...")
                    corr_result = await self._call_gateway_tool(
                        "aws-api-mcp___call_aws",
                        {
                            "cli_command": "aws cloudwatch describe-alarms --state-value ALARM --max-records 10",
                            "account_name": account_name,
                            "region": AWS_REGION
                        }
                    )
                    if corr_result.get("success"):
                        corr_text = str(corr_result.get("output", ""))
                        # Extract alarm names from same namespace
                        ns = metadata.get("namespace", "")
                        corr_alarm_names = re.findall(r'"AlarmName":\s*"([^"]+)"', corr_text)
                        related_alarms = [a for a in corr_alarm_names if a != alarm_name][:3]
                        if related_alarms:
                            logger.info(f"[KB_SEARCH] Found {len(related_alarms)} related alarms: {related_alarms}")
            except Exception as corr_err:
                logger.warning(f"[KB_SEARCH] Alarm correlation skipped: {corr_err}")

            # Direct Bedrock KB retrieval
            kb = BedrockKBRetriever()

            if not kb.kb_id:
                logger.warning("No KB ID configured")
                return {
                    "success": True,
                    "response": "Knowledge Base not configured. Manual investigation recommended.",
                    "agent_type": "knowledge",
                    "kb_results": [],
                    "related_alarms": related_alarms
                }

            # Multi-query KB retrieval with fallback (Step 2)
            # Append related alarm names to query for better root-cause matching
            extra_context = " ".join(related_alarms) if related_alarms else ""
            fallback_result = kb.retrieve_with_fallback(
                service=metadata.get("service", ""),
                metric_name=metadata.get("metric_name", ""),
                alarm_name=(alarm_name or "") + (" " + extra_context if extra_context else ""),
                namespace=metadata.get("namespace", ""),
                max_results=5
            )

            kb_results = fallback_result.get("results", [])
            kb_confidence = fallback_result.get("confidence", "none")

            if not kb_results:
                return {
                    "success": True,
                    "response": f"No relevant KB results found (tried multiple query strategies). Manual investigation recommended.",
                    "agent_type": "knowledge",
                    "kb_results": [],
                    "kb_confidence": kb_confidence,
                    "related_alarms": related_alarms
                }
            
            # Format as concise numbered steps (top 3 results)
            steps = []
            for i, result in enumerate(kb_results[:3], 1):
                content = result['content']
                score = result['score']

                # Strip XML tags (<step>, <rollback>, etc.) for human display
                display_content = re.sub(r'</?(?:step|rollback)[^>]*>', '', content)
                # Collapse multiple blank lines
                display_content = re.sub(r'\n{3,}', '\n\n', display_content).strip()
                # Truncate for display (after stripping tags)
                display_content = display_content[:1000]

                # Extract meaningful title from first line
                first_line = display_content.split('\n')[0].strip().strip('#').strip()
                title = first_line[:80] if len(first_line) > 10 else f"Result {i}"

                # Extract source document name from URI
                source = ""
                if result.get('source_uri'):
                    source_name = result['source_uri'].rsplit('/', 1)[-1]
                    source = f" | Source: {source_name}"

                steps.append(f"**{title}** (relevance: {score:.0%}{source}):\n{display_content}")

            kb_output = "\n\n".join(steps)
            
            logger.info(f"Step 2 complete: Found {len(kb_results)} KB results (confidence: {kb_confidence})")

            confidence_note = f"\n\n*KB confidence: {kb_confidence}*" if kb_confidence != "high" else ""
            related_note = f"\n*Related active alarms: {', '.join(related_alarms)}*" if related_alarms else ""

            return {
                "success": True,
                "response": f"**Knowledge Base Remediation Steps**\n\n{kb_output}{confidence_note}{related_note}",
                "agent_type": "knowledge",
                "kb_results": kb_results,
                "kb_confidence": kb_confidence,
                "related_alarms": related_alarms
            }
            
        except Exception as e:
            logger.error(f"Step 2 failed: {e}")
            return {
                "success": False,
                "response": f"KB search failed: {str(e)}",
                "agent_type": "error",
                "kb_results": []
            }
    
    async def approve_remediation(
        self,
        workflow_id: str,
        query: str,
        account_name: str,
        step_results: Dict[str, Any],
        use_dynamic: bool = True,
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Step 3: Generate, validate, and execute a KB-driven remediation plan.

        Five-phase pipeline:
          Phase 1 — Plan generation: Supervisor Runtime receives KB prose and produces
                    an XML plan of AWS CLI commands (<step> tags).
          Phase 2 — XML parsing: _parse_xml_steps() extracts commands with optional
                    on_success/on_failure conditional attributes.
          Phase 2.5 — Deterministic guard: validate_steps() (guard_policies.py) rejects
                    known-dangerous operations instantly (e.g., delete-db-instance) before
                    spending an LLM call on guard validation.
          Phase 2.6 — Rollback generation: Supervisor generates <rollback> inverses for
                    each step so operators can undo changes manually if needed.
          Phase 3 — LLM guard validation: Supervisor evaluates the full step list against
                    the KB context.  Prompt strictness varies by GUARD_MODE:
                    "production" requires --dry-run for EC2/RDS and rejects irreversible ops;
                    "demo" only blocks data-deletion commands.
          Phase 4 — Execution: Approved CLI commands are sent to the Gateway HTTP endpoint
                    (aws-api-mcp MCP target) one by one.  Conditional steps (on_success,
                    on_failure) are evaluated against a step_outcomes dict.  Unconditional
                    step failures trigger fail-fast (break); conditional plans continue.

        Args:
            workflow_id: UUID of the current workflow.
            query: CloudWatch agent response text used for alarm name extraction.
            account_name: Customer AWS account name for cross-account CLI execution.
            step_results: Accumulated results containing kb_search.kb_results from Step 2.
            use_dynamic: Reserved for future A/B testing; currently unused.
            session_id: AgentCore Memory session ID for plan/guard/rollback calls.

        Returns:
            Dict with keys: success, response, agent_type, execution_log (list),
            rollback_steps (list of str).
        """
        if not SUPERVISOR_RUNTIME_ARN:
            return {
                "success": False,
                "response": "Remediation requires AgentCore Runtime - SUPERVISOR_RUNTIME_ARN not configured",
                "agent_type": "error"
            }

        try:
            logger.info(f"[REMEDIATION] Step 3: Executing KB-driven remediation for workflow {workflow_id}")
            
            agentcore = get_agentcore_client(region=AWS_REGION)
            
            # Get KB results from Step 2 (full results, not truncated display)
            kb_data = step_results.get("kb_search", {})
            kb_results = kb_data.get("kb_results", [])
            
            if not kb_results:
                return {
                    "success": False,
                    "response": "No KB results available from Step 2. Cannot generate remediation plan.",
                    "agent_type": "error"
                }
            
            # Extract alarm name and resolve resource ID via Gateway
            alarm_name = self._extract_alarm_name(query)
            resource_id = None
            namespace = None
            alarm_details = None
            
            if alarm_name:
                logger.info(f"[REMEDIATION] Resolving resource ID for alarm: {alarm_name}")
                
                # Use ResourceIDResolver with Supervisor LLM extraction
                resolver = ResourceIDResolver()
                resolved = await resolver.resolve(
                    alarm_name=alarm_name,
                    account_name=account_name,
                    region=AWS_REGION,
                    gateway_caller=self._call_gateway_tool,
                    cloudwatch_text=query,  # Pass CloudWatch response text for LLM extraction
                    agentcore_client=agentcore  # Pass AgentCore client for Supervisor calls
                )
                
                resource_id = resolved.get("resource_id")
                namespace = resolved.get("namespace")
                alarm_details = resolved.get("alarm_details")
                
                if resolved.get("error"):
                    logger.warning(f"[REMEDIATION] Resource resolution error: {resolved['error']}")

                if resource_id:
                    logger.info(f"[REMEDIATION] Resolved resource ID: {resource_id}")
                else:
                    logger.warning("[REMEDIATION] Could not resolve resource ID, will use alarm name")
            
            # Phase 1: Generate XML plan from KB via Supervisor Runtime
            # Strip XML tags from KB content — give Supervisor prose + CLI text, not XML structure
            kb_prose = "\n\n".join([
                f"KB Result {i+1}:\n{re.sub(r'</?(?:step|rollback)[^>]*>', '', r['content']).strip()}"
                for i, r in enumerate(kb_results[:3])
            ])

            plan_prompt = f"""Based on this KB guidance, generate ONLY the REMEDIATION AWS CLI commands (not diagnosis or verification steps).

Alarm: {alarm_name or 'Unknown'}
Resource: {resource_id or 'Unknown'}

--- KB REFERENCE (do not copy verbatim) ---
{kb_prose}
--- END KB REFERENCE ---

Generate a focused remediation plan with these rules:
1. Include ONLY commands that FIX the issue (modify, update, scale, etc.)
2. Do NOT include diagnostic commands (describe, get, list) unless they are a prerequisite for a remediation command
3. Do NOT include verification commands (describe-alarms for state check) — verification is handled separately
4. Use the resolved resource ID: {resource_id or 'RESOURCE_ID'}
5. Use AWS CLI shorthand syntax for complex parameters (NOT JSON)

Output format:
<step>aws service action --param value</step>
<step on_success="1">aws service action --param value</step>

- on_success="N" means: only run if step N succeeded
- on_failure="N" means: only run if step N failed

If NO executable remediation commands found, output: NO_STEPS_FOUND"""
            
            logger.info("[REMEDIATION] Generating remediation plan via Supervisor Runtime...")
            plan_result = await agentcore.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": plan_prompt,
                    "account_name": account_name,
                    "session_id": session_id or f"workflow-{workflow_id}-plan"
                }
            )
            
            plan_response = plan_result.get("response", "")
            
            if "NO_STEPS_FOUND" in plan_response:
                return {
                    "success": False,
                    "response": "KB guidance found but no executable steps identified.",
                    "agent_type": "supervisor"
                }

            # Phase 2: Parse XML steps locally
            steps = self._parse_xml_steps(plan_response)

            if not steps:
                return {
                    "success": False,
                    "response": "Could not parse executable steps from KB guidance.",
                    "agent_type": "supervisor"
                }

            logger.info(f"[REMEDIATION] Generated plan with {len(steps)} steps")

            # Phase 2.5: Deterministic guard validation runs synchronously (no LLM call).
            # GUARD_MODE ("demo" or "production") is loaded from config_loader and controls
            # which extra flags (e.g. --dry-run) are required in production mode.
            policy_approved, policy_reason = validate_steps(steps, mode=GUARD_MODE)
            if not policy_approved:
                return {
                    "success": False,
                    "response": f"Remediation blocked by policy:\n\n{policy_reason}",
                    "agent_type": "guard_policy"
                }
            logger.info("[REMEDIATION] Deterministic guard passed")

            # Phase 2.6: Generate rollback plan via Supervisor
            rollback_steps = []
            try:
                rollback_prompt = f"""For each AWS CLI command below, generate the INVERSE command that would undo the operation.
Return each inverse command in <rollback>...</rollback> tags.

Commands:
{json.dumps([s['cli_command'] for s in steps], indent=2)}

Example:
Input: aws lambda put-function-concurrency --function-name myFunc --reserved-concurrent-executions 100
Output: <rollback>aws lambda delete-function-concurrency --function-name myFunc</rollback>

If a command cannot be reversed, output: <rollback>MANUAL: describe original state before applying</rollback>"""

                rollback_result = await agentcore.invoke_runtime(
                    runtime_arn=SUPERVISOR_RUNTIME_ARN,
                    payload={
                        "prompt": rollback_prompt,
                        "account_name": account_name,
                        "session_id": session_id or f"workflow-{workflow_id}-rollback"
                    }
                )
                rollback_response = rollback_result.get("response", "")
                rollback_steps = re.findall(r'<rollback>(.*?)</rollback>', rollback_response, re.DOTALL)
                rollback_steps = [s.strip() for s in rollback_steps]
                logger.info(f"[REMEDIATION] Generated {len(rollback_steps)} rollback steps")
            except Exception as rb_err:
                logger.warning(f"[REMEDIATION] Rollback generation skipped: {rb_err}")

            # Phase 3: Batch guard validation with KB context (Option A - all steps in one call)
            steps_json = json.dumps([s["cli_command"] for s in steps], indent=2)

            # Include KB context for validation (FE-BE-decoupling pattern)
            kb_context = "\n\n".join([f"KB Source {i+1}:\n{r['content'][:500]}" for i, r in enumerate(kb_results[:3])])

            # Phase 3 guard prompt: production mode enforces strict safety criteria
            # (--dry-run, reversibility, blast radius); demo mode only blocks genuinely
            # destructive operations to allow realistic testing without false rejections.
            if GUARD_MODE == "production":
                guard_prompt = f"""Validate these AWS operations for PRODUCTION safety.

**KB Documentation Context:**
{kb_context}

**Proposed Operations:**
{steps_json}

**Production Validation Criteria (STRICT):**
REJECT if ANY of these apply:
- Operations lack --dry-run where supported (EC2, RDS)
- Operations have high blast radius (affect multiple resources)
- Operations are not easily reversible
- Operations modify IAM, security groups, or network ACLs
- Operations delete ANY resource
- Operations disable monitoring, logging, or encryption

APPROVE ONLY if operations are:
- Targeted to a single, specific resource
- Easily reversible (configuration changes, not deletions)
- Standard operational actions (scaling, redeployment, config updates)
- Include appropriate safety flags (--dry-run, --no-execute)

Reply with: APPROVED or REJECTED with detailed reasoning."""
            else:
                guard_prompt = f"""Validate these AWS operations for safety. This is a DEMO environment - be lenient.

**KB Documentation Context:**
{kb_context}

**Proposed Operations:**
{steps_json}

**Validation Criteria (Demo-friendly):**
ONLY reject if operations involve:
- Deleting databases, tables, or storage (DELETE operations)
- Terminating EC2 instances or destroying infrastructure
- Disabling security controls or encryption
- Modifying IAM policies or roles

APPROVE operations that:
- Update API Gateway policies or configurations (safe, reversible)
- Create deployments or update resources (standard operations)
- Modify CloudWatch alarms or metrics
- Update application configurations

This is a demo/test environment. Focus on blocking truly destructive operations only.

Reply with: APPROVED (if operations are safe for demo) or REJECTED (if truly dangerous like data deletion)

Keep reasoning brief."""

            logger.info("[REMEDIATION] Validating steps via Supervisor guard...")
            guard_result = await agentcore.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": guard_prompt,
                    "account_name": account_name,
                    "session_id": session_id or f"workflow-{workflow_id}-guard"
                }
            )

            guard_response = guard_result.get("response", "")

            if "REJECTED" in guard_response:
                return {
                    "success": False,
                    "response": f"Supervisor rejected remediation steps as unsafe:\n\n{guard_response}",
                    "agent_type": "supervisor"
                }

            logger.info("[REMEDIATION] All steps approved by supervisor guard")

            # Check Gateway is configured
            if not GATEWAY_URL:
                return {
                    "success": False,
                    "response": "GATEWAY_URL not configured - cannot execute remediation",
                    "agent_type": "error"
                }
            
            # Phase 4: Execute each CLI command via the Gateway HTTP → aws-api-mcp MCP target.
            # step_outcomes tracks the outcome of each 1-based step index so that
            # on_success/on_failure conditions on later steps can be evaluated correctly.
            execution_log = []
            step_outcomes = {}  # step_number (1-based) → "success" | "failed" | "skipped"

            for i, step_dict in enumerate(steps, 1):
                cli_command = step_dict["cli_command"]
                on_success = step_dict.get("on_success")
                on_failure = step_dict.get("on_failure")

                # Evaluate conditions
                if on_success is not None and step_outcomes.get(on_success) != "success":
                    logger.info(f"[REMEDIATION] Step {i} skipped: on_success={on_success} not met")
                    step_outcomes[i] = "skipped"
                    execution_log.append({
                        'step': {'cli_command': cli_command},
                        'status': 'skipped',
                        'reason': f'Condition on_success={on_success} not met'
                    })
                    continue

                if on_failure is not None and step_outcomes.get(on_failure) != "failed":
                    logger.info(f"[REMEDIATION] Step {i} skipped: on_failure={on_failure} not met")
                    step_outcomes[i] = "skipped"
                    execution_log.append({
                        'step': {'cli_command': cli_command},
                        'status': 'skipped',
                        'reason': f'Condition on_failure={on_failure} not met'
                    })
                    continue

                logger.info(f"[REMEDIATION] Executing step {i}/{len(steps)}: {cli_command}")

                try:
                    exec_result = await self._call_gateway_tool(
                        "aws-api-mcp___call_aws",
                        {
                            "cli_command": cli_command,
                            "account_name": account_name,
                            "region": AWS_REGION
                        }
                    )

                    if exec_result.get("success"):
                        output = exec_result.get("output", {})
                        logger.info(f"[REMEDIATION] Step {i} executed successfully")
                        step_outcomes[i] = "success"
                        execution_log.append({
                            'step': {'cli_command': cli_command},
                            'status': 'success',
                            'result': str(output)[:200]
                        })
                    else:
                        error_msg = exec_result.get("error", "Unknown error")
                        logger.error(f"[REMEDIATION] Step {i} failed: {error_msg}")
                        step_outcomes[i] = "failed"
                        execution_log.append({
                            'step': {'cli_command': cli_command},
                            'status': 'failed',
                            'error': error_msg[:200]
                        })
                        # Fail-fast only for unconditional steps: conditional plans (on_success/
                        # on_failure attributes) are designed to handle failures gracefully,
                        # so we let them continue to evaluate remaining branches.
                        if on_success is None and on_failure is None:
                            break

                except Exception as e:
                    logger.error(f"[REMEDIATION] Step {i} failed: {e}")
                    step_outcomes[i] = "failed"
                    execution_log.append({
                        'step': {'cli_command': cli_command},
                        'status': 'failed',
                        'error': str(e)
                    })
                    if on_success is None and on_failure is None:
                        break
            
            # Format response
            executed = [log for log in execution_log if log['status'] != 'skipped']
            completed = all(log['status'] == 'success' for log in executed) if executed else False
            steps_summary = "\n".join([
                f"{i+1}. `{log['step']['cli_command']}` -- **{log['status'].upper()}**" +
                (f"\n   Error: {log.get('error', '')}" if log['status'] == 'failed' else "") +
                (f"\n   Reason: {log.get('reason', '')}" if log['status'] == 'skipped' else "")
                for i, log in enumerate(execution_log)
            ])

            status_text = "Complete" if completed else "Partial (some steps failed or skipped)"

            logger.info(f"[REMEDIATION] Remediation execution: {status_text}")

            return {
                "success": completed,
                "response": f"**KB-Driven Remediation: {status_text}**\n\nSteps Executed:\n{steps_summary}",
                "agent_type": "supervisor",
                "execution_log": execution_log,
                "rollback_steps": rollback_steps
            }

        except Exception as e:
            logger.error(f"Step 3 failed: {e}")
            return {
                "success": False,
                "response": f"Remediation failed: {str(e)}",
                "agent_type": "error"
            }
    
    async def close_jira_ticket(
        self,
        workflow_id: str,
        query: str,
        account_name: str,
        step_results: Dict[str, Any],
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Step 5: Close the Jira ticket created in Step 1 with a remediation summary.

        The closure prompt uses a structured 6-step instruction format because the Jira
        Supervisor agent must: (1) extract the ticket key, (2) add an ADF-formatted
        comment, (3) discover available transitions, (4) find the correct "Done/Resolved"
        transition ID (which varies per project), (5) execute the transition, and
        (6) confirm.  Jira Cloud API v3 requires ADF (Atlassian Document Format) for
        comment bodies — plain text is rejected with a 400 error.

        Args:
            workflow_id: UUID of the current workflow.
            query: Original user query (used as fallback context).
            account_name: Customer AWS account name.
            step_results: Must contain:
              - step_results["jira"]["response"] — ticket creation response with ticket key
              - step_results["remediation"]["execution_log"] — list of executed steps
            session_id: AgentCore Memory session ID.

        Returns:
            Dict with keys: success (bool), response (str), agent_type (str).
        """
        if not SUPERVISOR_RUNTIME_ARN:
            return {
                "success": False,
                "response": "Ticket closure requires AgentCore Runtime - SUPERVISOR_RUNTIME_ARN not configured",
                "agent_type": "error"
            }

        try:
            logger.info(f"Step 4: Closing Jira ticket for workflow {workflow_id}")
            
            agentcore = get_agentcore_client(region=AWS_REGION)
            
            # Get Jira ticket info from step 1
            jira_response = step_results.get("jira", {}).get("response", "")
            
            # Get remediation execution log from step 3
            remediation_data = step_results.get("remediation", {})
            execution_log = remediation_data.get("execution_log", [])
            
            if not jira_response:
                return {
                    "success": False,
                    "response": "No Jira ticket found to close - Step 1 may have failed",
                    "agent_type": "error"
                }
            
            # Format execution summary
            if execution_log:
                steps_summary = "\n".join([
                    f"{i+1}. `{log['step']['cli_command']}` -- {log['status'].upper()}"
                    for i, log in enumerate(execution_log)
                ])
            else:
                steps_summary = "No steps executed"
            
            success = remediation_data.get("success", False)
            status_text = "RESOLVED" if success else "ATTEMPTED"
            
            # Build ADF-formatted comment body (Jira API v3 requirement)
            comment_text = f"AUTOMATED REMEDIATION {status_text}. Steps Executed: {steps_summary}"
            comment_body_json = json.dumps({
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment_text}]
                    }]
                }
            })
            
            # Build prompt to close the ticket with explicit ADF format and transition discovery
            prompt = f"""Use the manage_jira tool to close the Jira ticket. Follow these steps EXACTLY:

Ticket creation response:
{jira_response[:300]}

**Step 1: Extract ticket key**
Find the ticket key (e.g., MD-123) from the creation response above.

**Step 2: Add comment with ADF format**
Call addComment with issueIdOrKey=<ticket_key> and this EXACT body:
{comment_body_json}

**Step 3: Get available transitions**
Call getTransitions with issueIdOrKey=<ticket_key> to get the list of available transitions.

**Step 4: Find Done/Resolved transition ID**
From the transitions response, find the transition with name "Done" or "Resolved" or "Closed".
Extract its "id" field (e.g., "31", "41", "51").

**Step 5: Execute transition**
Call transitionIssue with issueIdOrKey=<ticket_key> and body:
{{"transition": {{"id": "<transition_id_from_step_4>"}}}}

**Step 6: Confirm**
Return the ticket key and confirmation that it was closed.

Execute ALL steps in sequence. Do not skip any step."""
            
            result = await agentcore.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": prompt,
                    "account_name": account_name,
                    "session_id": session_id or f"workflow-{workflow_id}-closure"
                }
            )
            
            response = result.get("response", "")
            agent_type = result.get("agent_type", "jira")
            
            logger.info("Step 4 complete: Jira ticket closed")
            
            return {
                "success": True,
                "response": response,
                "agent_type": agent_type
            }
            
        except Exception as e:
            logger.error(f"Step 4 failed: {e}")
            return {
                "success": False,
                "response": f"Ticket closure failed: {str(e)}",
                "agent_type": "error"
            }
    
    async def approve_verification(
        self,
        workflow_id: str,
        query: str,
        account_name: str,
        step_results: Dict[str, Any],
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Step 4: Poll CloudWatch to verify the alarm has returned to OK after remediation.

        Calls `aws cloudwatch describe-alarms --alarm-names <name>` via the Gateway
        MCP target and checks the StateValue field in the response.  Retries up to
        VERIFICATION_MAX_RETRIES times with VERIFICATION_RETRY_DELAY_SECONDS between
        attempts (both configurable in config_loader.py) to account for the propagation
        delay between CLI execution and CloudWatch alarm state evaluation.

        This step always returns success=True (even when the alarm is still in ALARM)
        so the workflow can proceed to ticket closure regardless of verification outcome.
        The verified flag in the response tells the operator whether remediation was
        confirmed effective.

        Args:
            workflow_id: UUID of the current workflow.
            query: CloudWatch agent response text used to extract the alarm name.
            account_name: Customer AWS account name for cross-account CLI call.
            step_results: Accumulated results (for context; not directly used here).
            session_id: AgentCore Memory session ID.

        Returns:
            Dict with keys: success (bool), response (str), agent_type,
            alarm_state (str: "OK" | "ALARM" | "UNKNOWN" | "error"), verified (bool).
        """
        try:
            logger.info(f"Step 4: Verifying alarm state for workflow {workflow_id}")

            alarm_name = self._extract_alarm_name(query)
            if not alarm_name:
                logger.warning("[VERIFICATION] Could not extract alarm name, skipping verification")
                return {
                    "success": True,
                    "response": "Verification skipped: could not extract alarm name from CloudWatch response.",
                    "agent_type": "verification",
                    "alarm_state": "unknown",
                    "verified": False
                }

            if not GATEWAY_URL:
                return {
                    "success": True,
                    "response": "Verification skipped: GATEWAY_URL not configured.",
                    "agent_type": "verification",
                    "alarm_state": "unknown",
                    "verified": False
                }

            cli_command = f"aws cloudwatch describe-alarms --alarm-names {alarm_name}"

            for attempt in range(1, VERIFICATION_MAX_RETRIES + 1):
                logger.info(f"[VERIFICATION] Attempt {attempt}/{VERIFICATION_MAX_RETRIES}: checking alarm state")

                result = await self._call_gateway_tool(
                    "aws-api-mcp___call_aws",
                    {
                        "cli_command": cli_command,
                        "account_name": account_name,
                        "region": AWS_REGION
                    }
                )

                if result.get("success"):
                    output_text = str(result.get("output", ""))

                    # Check for OK state
                    if '"OK"' in output_text or "'OK'" in output_text or "StateValue: OK" in output_text:
                        logger.info(f"[VERIFICATION] Alarm '{alarm_name}' returned to OK state")
                        return {
                            "success": True,
                            "response": f"Alarm `{alarm_name}` has returned to **OK** state after remediation (verified on attempt {attempt}).",
                            "agent_type": "verification",
                            "alarm_state": "OK",
                            "verified": True
                        }

                    # Extract current state for reporting
                    state_match = re.search(r'"StateValue":\s*"(\w+)"', output_text)
                    current_state = state_match.group(1) if state_match else "UNKNOWN"
                    logger.info(f"[VERIFICATION] Alarm still in {current_state} state (attempt {attempt})")

                if attempt < VERIFICATION_MAX_RETRIES:
                    logger.info(f"[VERIFICATION] Waiting {VERIFICATION_RETRY_DELAY_SECONDS}s before retry...")
                    await asyncio.sleep(VERIFICATION_RETRY_DELAY_SECONDS)

            # Exhausted retries
            logger.warning(f"[VERIFICATION] Alarm '{alarm_name}' did not return to OK after {VERIFICATION_MAX_RETRIES} attempts")
            return {
                "success": True,
                "response": f"Alarm `{alarm_name}` is still in **{current_state}** state after {VERIFICATION_MAX_RETRIES} checks ({VERIFICATION_RETRY_DELAY_SECONDS}s apart). Manual investigation may be needed.",
                "agent_type": "verification",
                "alarm_state": current_state,
                "verified": False
            }

        except Exception as e:
            logger.error(f"Step 4 (verification) failed: {e}")
            return {
                "success": True,
                "response": f"Verification encountered an error: {str(e)}. Proceeding to closure.",
                "agent_type": "verification",
                "alarm_state": "error",
                "verified": False
            }

    async def execute_full_automation(
        self,
        workflow_id: str,
        query: str,
        cloudwatch_response: str,
        account_name: str,
        progress_callback=None,
        use_dynamic: bool = True,
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Execute all 5 workflow steps end-to-end without per-step human approval.

        Steps 1 (Jira) and 2 (KB search) run concurrently via asyncio.gather() because
        the KB search does not depend on the Jira result.  This saves 15-30 s compared
        to sequential execution.  Exceptions from either parallel task are caught as
        values (return_exceptions=True) and converted to error result dicts so a failure
        in one step does not prevent the other from completing.

        progress_callback, if provided, is called after each step completes with the
        current results dict.  The callback detects status changes by comparing against
        previous step statuses (handled in _process_workflow_step_async in routes.py).

        Args:
            workflow_id: UUID of the current workflow.
            query: Original user query (used for Jira description and ticket closure).
            cloudwatch_response: CloudWatch agent response (used for alarm extraction
                                 in KB search, remediation, and verification steps).
            account_name: Customer AWS account name.
            progress_callback: Optional callable(results_dict) invoked after each step
                               with the current accumulated results.
            use_dynamic: Reserved for future use; passed through to approve_remediation.
            session_id: AgentCore Memory session ID for all sub-step invocations.

        Returns:
            Dict with keys: workflow_id, steps (list), success (bool),
            message (str), error (str|None).
        """
        results = {
            "workflow_id": workflow_id,
            "steps": [],
            "success": False,
            "error": None
        }
        
        if not SUPERVISOR_RUNTIME_ARN:
            results["error"] = "AgentCore Runtime not configured - set SUPERVISOR_RUNTIME_ARN"
            results["message"] = "Full automation requires AgentCore Runtime"
            return results
        
        # Alarm detection already validated before reaching here (in routes.py)
        # No need to check _last_workflow_result.has_alarm
        
        def update_progress(step_info):
            results["steps"].append(step_info)
            if progress_callback:
                progress_callback(results)
        
        try:
            # Accumulate step results for passing to subsequent steps
            step_results = {}
            
            # Steps 1 + 2 run in PARALLEL via asyncio.gather().
            # KB search is independent of Jira — no data flows from Step 1 to Step 2.
            # asyncio.gather(return_exceptions=True) prevents one failure from cancelling
            # the other task; each result is checked individually below.
            update_progress({
                "step": "jira",
                "status": "executing",
                "message": "Creating Jira ticket + searching KB simultaneously...",
                "step_num": 1
            })
            results["steps"].append({
                "step": "kb",
                "status": "executing",
                "message": "Searching Bedrock Knowledge Base (parallel with Jira)...",
                "step_num": 2
            })
            if progress_callback:
                progress_callback(results)
            
            # Run Jira and KB search concurrently
            jira_result, kb_result = await asyncio.gather(
                self.approve_workflow(workflow_id, query, account_name, {}, cloudwatch_response=cloudwatch_response, session_id=session_id),
                self.approve_kb_search(workflow_id, cloudwatch_response, account_name, {}, session_id=session_id),
                return_exceptions=True
            )
            
            # Handle exceptions from gather (return_exceptions=True returns them as values)
            if isinstance(jira_result, Exception):
                logger.error(f"Jira step failed in parallel: {jira_result}")
                jira_result = {"success": False, "response": f"Jira failed: {str(jira_result)}", "agent_type": "error"}
            if isinstance(kb_result, Exception):
                logger.error(f"KB step failed in parallel: {kb_result}")
                kb_result = {"success": False, "response": f"KB search failed: {str(kb_result)}", "agent_type": "error", "kb_results": []}
            
            step_results["jira"] = jira_result
            step_results["kb_search"] = kb_result
            
            # Update progress for both completed steps
            results["steps"][-2].update({"status": "completed", "result": jira_result.get("response", "")})
            results["steps"][-1].update({"status": "completed", "result": kb_result.get("response", "")})
            if progress_callback:
                progress_callback(results)
            
            # Step 3: Remediation
            update_progress({
                "step": "remediation",
                "status": "executing",
                "message": "Executing KB-driven remediation...",
                "step_num": 3
            })
            
            # Use CloudWatch response (not user query) for alarm extraction
            remediation_result = await self.approve_remediation(workflow_id, cloudwatch_response, account_name, step_results, use_dynamic, session_id=session_id)
            step_results["remediation"] = remediation_result
            
            results["steps"][-1].update({
                "status": "completed",
                "result": remediation_result.get("response", "")
            })
            if progress_callback:
                progress_callback(results)

            # Step 4: Verification
            update_progress({
                "step": "verification",
                "status": "executing",
                "message": "Verifying alarm state...",
                "step_num": 4
            })

            verification_result = await self.approve_verification(
                workflow_id, cloudwatch_response, account_name, step_results, session_id=session_id
            )
            step_results["verification"] = verification_result

            results["steps"][-1].update({
                "status": "completed",
                "result": verification_result.get("response", "")
            })
            if progress_callback:
                progress_callback(results)

            # Step 5: Ticket closure
            update_progress({
                "step": "closure",
                "status": "executing",
                "message": "Closing Jira ticket...",
                "step_num": 5
            })
            
            closure_result = await self.close_jira_ticket(workflow_id, query, account_name, step_results, session_id=session_id)
            step_results["closure"] = closure_result
            
            results["steps"][-1].update({
                "status": "completed",
                "result": closure_result.get("response", "")
            })
            if progress_callback:
                progress_callback(results)
            
            results["success"] = True
            results["message"] = "Full automation completed via AgentCore Runtime"

        except Exception as e:
            results["success"] = False
            results["error"] = str(e)
            results["message"] = f"Automation failed: {str(e)}"
        
        return results
    
