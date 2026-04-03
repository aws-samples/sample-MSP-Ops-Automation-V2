"""
Resource ID Resolver Service - Supervisor-Backed

Resolves AWS resource IDs from CloudWatch alarm details using Supervisor LLM
to extract resource information and Gateway to execute AWS API calls.

Flow:
1. Supervisor extracts resource info (namespace, name, CLI command) from CloudWatch text
2. Gateway executes CLI command to list/describe the resource
3. Supervisor extracts the actual resource ID from API response
"""

import json
import logging
import os
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)

# AgentCore Runtime configuration
SUPERVISOR_RUNTIME_ARN = os.getenv("SUPERVISOR_RUNTIME_ARN", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


class ResourceIDResolver:
    """
    Resolve AWS resource IDs from CloudWatch alarms using Supervisor LLM.
    
    All API calls route through Gateway → aws-api-mcp → credential_helper
    for cross-account access.
    """
    
    async def resolve(
        self,
        alarm_name: str,
        account_name: str,
        region: str,
        gateway_caller: Callable,
        cloudwatch_text: str = "",
        agentcore_client = None
    ) -> Dict[str, Any]:
        """
        Main entry: Use Supervisor to extract resource info and resolve ID
        
        Args:
            alarm_name: CloudWatch alarm name
            account_name: Customer account for cross-account access
            region: AWS region
            gateway_caller: async callable(tool_name, arguments) → dict with success/output/error
            cloudwatch_text: CloudWatch response text to parse
            agentcore_client: AgentCoreClient instance for Supervisor calls
            
        Returns:
            dict with namespace, resource_name, resource_id, error
        """
        logger.info(f"[RESOLVE_ID] Resolving resource ID for alarm: {alarm_name}")
        
        if not cloudwatch_text:
            return {
                "resource_id": None,
                "error": "No CloudWatch text provided for resource extraction"
            }
        
        if not SUPERVISOR_RUNTIME_ARN:
            return {
                "resource_id": None,
                "error": "SUPERVISOR_RUNTIME_ARN not configured - cannot use LLM extraction"
            }
        
        # Import here to avoid circular dependency
        if agentcore_client is None:
            from app.core.agentcore_client import AgentCoreClient
            agentcore_client = AgentCoreClient(region=region)
        
        try:
            # Phase 1 — LLM extraction: ask the Supervisor to parse the CloudWatch response
            # text and return a structured JSON object with namespace, resource_name, and
            # the AWS CLI command needed to list/describe resources of that type.
            # This avoids hard-coded per-service parsing logic and handles any service
            # the Supervisor knows about.
            logger.info("[RESOLVE_ID] Phase 1: Extracting resource info via Supervisor...")
            
            extract_prompt = f"""Extract AWS resource information from this CloudWatch alarm text.

CloudWatch Text:
{cloudwatch_text}

Return ONLY a JSON object with these fields:
{{
  "namespace": "AWS/ServiceName (e.g., AWS/ApiGateway, AWS/Lambda, AWS/EC2)",
  "resource_name": "the resource name mentioned in the text",
  "aws_cli_command": "AWS CLI command to list/describe this resource type (e.g., aws apigateway get-rest-apis)"
}}

Rules:
- namespace must be in format AWS/ServiceName
- resource_name should be the actual name from the text (e.g., aiops-demo-api, my-function, i-abc123)
- aws_cli_command should be the AWS CLI command to list or describe resources of this type
- Return ONLY valid JSON, no markdown, no explanation

Example for API Gateway:
{{"namespace": "AWS/ApiGateway", "resource_name": "my-api", "aws_cli_command": "aws apigateway get-rest-apis"}}"""
            
            extract_result = await agentcore_client.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": extract_prompt,
                    "account_name": account_name,
                    "session_id": f"resolve-extract-{alarm_name}"
                }
            )
            
            extract_response = extract_result.get("response", "")
            
            # Parse JSON from response (handle markdown code blocks)
            resource_info = self._parse_json_from_text(extract_response)
            
            if not resource_info:
                logger.error(f"[RESOLVE_ID] Failed to parse resource info from Supervisor response")
                return {
                    "resource_id": None,
                    "error": "Could not extract resource info from CloudWatch text"
                }
            
            namespace = resource_info.get("namespace", "")
            resource_name = resource_info.get("resource_name", "")
            cli_command = resource_info.get("aws_cli_command", "")
            
            logger.info(f"[RESOLVE_ID] Extracted: namespace={namespace}, resource_name={resource_name}")
            
            if not namespace or not resource_name or not cli_command:
                return {
                    "resource_id": None,
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "error": "Incomplete resource info extracted"
                }
            
            # Short-circuit for services where the resource name is also its identifier.
            # For S3 (bucket name), ECS (cluster/service name), and DynamoDB (table name)
            # there is no separate "resource ID" field — Phase 2 CLI call is unnecessary.
            if namespace in ["AWS/S3", "AWS/ECS", "AWS/DynamoDB"]:
                logger.info(f"[RESOLVE_ID] {namespace}: name IS the ID")
                return {
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "resource_id": resource_name,
                    "dimensions": [],
                    "additional_ids": {},
                    "alarm_details": None
                }
            
            # Phase 2 — CLI execution: call the AWS API via the Gateway MCP target to
            # list/describe resources of the extracted type.  The response is raw API
            # output (JSON text) that will be fed to the Supervisor in Phase 3.
            logger.info(f"[RESOLVE_ID] Phase 2: Executing CLI command via Gateway...")
            logger.info(f"[RESOLVE_ID] Command: {cli_command}")
            
            exec_result = await gateway_caller(
                "aws-api-mcp___call_aws",
                {
                    "cli_command": cli_command,
                    "account_name": account_name,
                    "region": region
                }
            )
            
            if not exec_result.get("success"):
                error_msg = exec_result.get("error", "Unknown error")
                logger.error(f"[RESOLVE_ID] CLI execution failed: {error_msg}")
                return {
                    "resource_id": None,
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "error": f"Failed to execute CLI command: {error_msg}"
                }
            
            # Get API response text
            output = exec_result.get("output", {})
            content = output.get("content", [])
            if not content:
                logger.error("[RESOLVE_ID] No content in CLI response")
                return {
                    "resource_id": None,
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "error": "Empty CLI response"
                }
            
            api_response_text = content[0].get("text", "") if isinstance(content, list) else ""
            if not api_response_text:
                logger.error("[RESOLVE_ID] Empty text in CLI response")
                return {
                    "resource_id": None,
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "error": "Empty CLI response text"
                }
            
            # Phase 3 — ID extraction: ask the Supervisor to find the specific resource ID
            # field within the CLI output.  The per-service instructions in the prompt
            # (API Gateway "id", Lambda "FunctionArn", EC2 "InstanceId", etc.) guide the
            # LLM to return the correct field without requiring service-specific parsing code.
            # The response is expected to be a single bare value (NOT JSON) to simplify cleanup.
            logger.info("[RESOLVE_ID] Phase 3: Extracting resource ID via Supervisor...")
            
            id_prompt = f"""Extract the resource ID from this AWS API response.

Resource Name to Find: {resource_name}
AWS Service: {namespace}

API Response:
{api_response_text[:2000]}

Find the resource with name "{resource_name}" and return ONLY its ID field.

For different services:
- API Gateway: return the "id" field
- Lambda: return the "FunctionArn" or "FunctionName"
- EC2: return the "InstanceId" (format: i-xxxxx)
- RDS: return the "DBInstanceIdentifier"
- SQS: return the "QueueUrl"

Return ONLY the ID value as plain text, no JSON, no explanation.
If the resource is not found, return: NOT_FOUND"""
            
            id_result = await agentcore_client.invoke_runtime(
                runtime_arn=SUPERVISOR_RUNTIME_ARN,
                payload={
                    "prompt": id_prompt,
                    "account_name": account_name,
                    "session_id": f"resolve-id-{alarm_name}"
                }
            )
            
            id_response = id_result.get("response", "").strip()
            
            if "NOT_FOUND" in id_response or not id_response:
                logger.warning(f"[RESOLVE_ID] Resource not found in API response: {resource_name}")
                return {
                    "resource_id": None,
                    "namespace": namespace,
                    "resource_name": resource_name,
                    "error": f"Resource '{resource_name}' not found in {namespace}"
                }
            
            # Clean up the response (remove quotes, markdown, etc.)
            resource_id = id_response.strip('"\'` \n')
            
            logger.info(f"[RESOLVE_ID] Resolved resource ID: {resource_id}")
            
            return {
                "namespace": namespace,
                "resource_name": resource_name,
                "resource_id": resource_id,
                "dimensions": [],
                "additional_ids": {},
                "alarm_details": None
            }
            
        except Exception as e:
            logger.error(f"[RESOLVE_ID] Exception during resolution: {e}")
            return {
                "resource_id": None,
                "error": f"Resolution failed: {str(e)}"
            }
    
    def _parse_json_from_text(self, text: str) -> Optional[Dict]:
        """
        Robustly extract a JSON object from LLM output that may contain extra formatting.

        LLMs frequently wrap JSON in markdown fences (```json ... ```) or include
        explanatory prose before/after the object.  Three parsing strategies are tried
        in order from most specific to least:

          1. Markdown code fence: extracts content between ```[json]...``` markers.
             Most reliable when the LLM follows the prompt's "Return ONLY valid JSON" instruction.
          2. Brace scan: finds the first {…} block in the text using a regex that handles
             one level of nesting.  Works when the LLM prepends a sentence like "Here is the JSON:".
          3. Full-text parse: treats the entire string as JSON.  Last resort for responses
             that are pure JSON with no surrounding text.

        Args:
            text: Raw Supervisor response string.

        Returns:
            Parsed dict, or None if all three strategies fail.
        """
        import re

        # Strategy 1: ```json ... ``` or ``` ... ``` code fence
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except (ValueError, json.JSONDecodeError):  # nosec B110
                pass

        # Strategy 2: find the first {…} block, allowing one level of nested braces
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except (ValueError, json.JSONDecodeError):  # nosec B110
                pass

        # Strategy 3: the whole text is valid JSON (no surrounding prose)
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):  # nosec B110
            pass
        
        logger.error(f"[PARSE_JSON] Failed to parse JSON from text: {text[:200]}...")
        return None