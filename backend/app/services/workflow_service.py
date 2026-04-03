# backend/app/services/workflow_service.py
"""
Workflow orchestration service — DynamoDB-backed state management layer.

Wraps CloudWatchJiraKBRemediationGraph (workflow_graph.py) with a persistence
layer that makes workflow state durable across ECS container restarts.

DynamoDB key conventions (all items share the CHAT_REQUESTS_TABLE table):
  "workflow-{workflow_id}"  — master workflow record (query, account, flags, step_results)
  "approval-{workflow_id}"  — pending approval record; deleted once a step is approved
                              or the workflow is rejected

Crash-safety pattern (put-then-delete):
  When advancing from one approval step to the next, the NEW approval item is written
  first and only then is the OLD one deleted.  This ensures that if the process crashes
  between the two DynamoDB calls, the approval is not silently lost — the next poll will
  find the new item and re-present it to the operator.

Step results accumulation:
  step_results is a dict keyed by step name ("jira", "kb_search", "remediation", etc.)
  and is persisted on the workflow DynamoDB item after each approved step.  Each
  subsequent step receives the full accumulated dict so it can reference earlier results
  (e.g., remediation reads kb_search.kb_results; closure reads jira.response).
"""

import os
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
import uuid
import boto3
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

from app.services.workflow_graph import CloudWatchJiraKBRemediationGraph
from app.core.workspace_context import get_workspace_context

# DynamoDB table for workflow state (reuses chat requests table)
TABLE_NAME = os.getenv("CHAT_REQUESTS_TABLE", "msp-assistant-chat-requests")
TTL_SECONDS = 3600  # 1 hour for workflow state

_dynamodb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
_table = _dynamodb.Table(TABLE_NAME)


def _decimal_to_python(obj):
    """Convert DynamoDB Decimal types to Python types."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    elif isinstance(obj, dict):
        return {k: _decimal_to_python(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decimal_to_python(i) for i in obj]
    return obj


class WorkflowService:
    """
    Service for workflow orchestration via REST API.
    Wraps existing workflow_graph.py logic with DynamoDB-backed state (multi-container safe).
    """
    
    def __init__(self):
        """Initialize workflow service with DynamoDB storage."""
        self.workspace = get_workspace_context()
    
    async def start_workflow(self, query: str, account_name: str, full_automation: bool = False, has_alarm: bool = False, cloudwatch_response: str = "", user_id: str = None, session_id: str = None) -> Dict:
        """
        Start a new workflow with session-aware routing.
        
        Args:
            query: User query
            account_name: Account name for session context
            full_automation: If True, show single confirmation for auto-execution
            has_alarm: If True, alarm was detected by caller (routes.py from AgentCore response)
            cloudwatch_response: CloudWatch agent response text (contains alarm names for KB search)
            
        Returns:
            Dict with workflow_id and initial response
        """
        try:
            # Get customer session if not default account
            customer_session = None
            if account_name and account_name != "default":
                self.workspace.set_current_account(account_name)
                customer_session = self.workspace.get_current_session()

            # Create session-aware workflow graph
            workflow_graph = CloudWatchJiraKBRemediationGraph(customer_session=customer_session)
            
            # Start workflow (graph returns stub, we use caller's has_alarm instead)
            workflow_id, result = workflow_graph.start_workflow(query)
            
            # Use has_alarm from caller (routes.py already detected from AgentCore response)
            logger.info(f"Workflow Service: has_alarm={has_alarm} (from caller)")
            
            # Persist the workflow record so any ECS container can look it up later.
            # The "workflow-{id}" key prefix distinguishes it from chat request items
            # and "approval-{id}" items that share the same table.
            now = time.time()
            workflow_item = {
                "request_id": f"workflow-{workflow_id}",
                "workflow_id": workflow_id,
                "query": query,
                "cloudwatch_response": cloudwatch_response,  # Store for KB search alarm extraction
                "account_name": account_name,
                "user_id": user_id or "unknown",  # Store for authorization check on approve/reject
                "session_id": session_id or f"workflow-{workflow_id}",  # Conversation session for STM continuity
                "created_at": datetime.now().isoformat(),
                "has_alarm": has_alarm,
                "full_automation": full_automation,
                "ttl": int(now + TTL_SECONDS)
            }
            _table.put_item(Item=workflow_item)
            
            # Check if approval needed
            if has_alarm:
                logger.info(f"Adding pending approval for workflow {workflow_id}")
                
                # Store pending approval in DynamoDB
                approval_data = {
                    "workflow_id": workflow_id,
                    "query": query,
                    "alarm_data": {
                        "has_alarms": True,
                        "alarm_count": 1
                    }
                }
                
                # Full automation mode: single confirmation card
                if full_automation:
                    approval_data["type"] = "full_auto"
                    approval_data["disclaimer"] = "This will automatically execute: Jira ticket creation → KB search → Remediation → Alarm verification → Ticket closure"
                else:
                    # Step-by-step mode: individual approval cards
                    approval_data["type"] = "jira"
                
                approval_item = {
                    "request_id": f"approval-{workflow_id}",
                    "workflow_id": workflow_id,
                    "approval_data": approval_data,
                    "created_at": int(now),
                    "ttl": int(now + TTL_SECONDS)
                }
                _table.put_item(Item=approval_item)
                logger.info("Pending approval stored in DynamoDB")
            else:
                logger.warning("No alarm detected - not adding to pending approvals")
            
            cloudwatch_response = ""
            if hasattr(result, 'results') and result.results:
                cw_result = result.results.get("cloudwatch")
                if cw_result is not None:
                    try:
                        cloudwatch_response = str(cw_result.result)
                    except (AttributeError, TypeError):
                        cloudwatch_response = str(cw_result)

            return {
                "success": True,
                "workflow_id": workflow_id,
                "requires_approval": has_alarm,
                "cloudwatch_response": cloudwatch_response
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Workflow start failed: {str(e)}"
            }
    
    async def get_pending_approvals(self) -> Dict:
        """
        Get list of workflows requiring approval from DynamoDB.
        
        Returns:
            Dict with pending approval list
        """
        try:
            # Scan for all approval items (request_id starts with "approval-")
            response = _table.scan(
                FilterExpression="begins_with(request_id, :prefix)",
                ExpressionAttributeValues={":prefix": "approval-"}
            )
            
            approvals = []
            for item in response.get("Items", []):
                approval_data = item.get("approval_data", {})
                # Convert Decimal types to Python types
                approval_data = _decimal_to_python(approval_data)
                approvals.append(approval_data)
            
            logger.info(f"Found {len(approvals)} pending approvals in DynamoDB")
            
            return {
                "success": True,
                "approvals": approvals
            }
        except Exception as e:
            logger.error(f"Error fetching pending approvals: {e}")
            return {
                "success": False,
                "approvals": []
            }
    
    async def approve_step(self, workflow_id: str, step_type: str, progress_callback=None) -> Dict:
        """
        Approve a workflow step and execute it.
        
        Args:
            workflow_id: Workflow identifier
            step_type: Step type (jira, kb_search, remediation, closure)
            
        Returns:
            Dict with step result and next approval
        """
        try:
            # Get workflow from DynamoDB
            workflow_response = _table.get_item(Key={"request_id": f"workflow-{workflow_id}"})
            workflow_item = workflow_response.get("Item")
            
            if not workflow_item:
                return {"success": False, "message": "Workflow not found"}
            
            workflow_item = _decimal_to_python(workflow_item)
            query = workflow_item.get("query")
            account_name = workflow_item.get("account_name")
            session_id = workflow_item.get("session_id", f"workflow-{workflow_id}")

            # step_results is the running accumulation of every completed step's output.
            # Each step appends its result via save_step_result() so later steps can
            # reference earlier outputs (e.g., remediation reads kb_search.kb_results).
            step_results = workflow_item.get("step_results", {})
            step_results = _decimal_to_python(step_results)
            
            # Recreate workflow graph with session
            customer_session = None
            if account_name and account_name != "default":
                self.workspace.set_current_account(account_name)
                customer_session = self.workspace.get_current_session()
            workflow_graph = CloudWatchJiraKBRemediationGraph(customer_session=customer_session)

            # Map step types to human-readable names
            step_names = {
                "jira": "Jira Ticket Creation",
                "kb_search": "Knowledge Base Search",
                "remediation": "Remediation Execution",
                "verification": "Alarm Verification",
                "closure": "Jira Ticket Closure"
            }
            
            # update_approval advances the workflow to the next step by writing the new
            # approval item BEFORE removing the old one.  If the process crashes between
            # these two DynamoDB calls, the new item is found on the next poll and the
            # operator is re-prompted — no silent data loss.  Passing None deletes the
            # approval item entirely, signalling workflow completion.
            def update_approval(next_type: str):
                if next_type:
                    now = time.time()
                    # Put new approval first, then delete old — crash-safe put-then-delete
                    _table.put_item(Item={
                        "request_id": f"approval-{workflow_id}",
                        "workflow_id": workflow_id,
                        "approval_data": {"type": next_type, "workflow_id": workflow_id},
                        "created_at": int(now),
                        "ttl": int(now + TTL_SECONDS)
                    })
                else:
                    _table.delete_item(Key={"request_id": f"approval-{workflow_id}"})
            
            # Helper to save step result to DynamoDB
            def save_step_result(step_key: str, step_result: Dict):
                # Convert floats to Decimal for DynamoDB compatibility
                def convert_floats(obj):
                    if isinstance(obj, float):
                        return Decimal(str(obj))
                    elif isinstance(obj, dict):
                        return {k: convert_floats(v) for k, v in obj.items()}
                    elif isinstance(obj, list):
                        return [convert_floats(i) for i in obj]
                    return obj
                
                # Add new result to accumulated results
                step_results[step_key] = step_result
                
                # Convert ALL accumulated results (including old KB floats) to Decimal
                converted_results = convert_floats(step_results)
                
                # Add 20-min TTL for step results
                now = time.time()
                ttl = int(now + 1200)  # 20 minutes
                
                _table.update_item(
                    Key={"request_id": f"workflow-{workflow_id}"},
                    UpdateExpression="SET step_results = :sr, #ttl = :ttl",
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues={":sr": converted_results, ":ttl": ttl}
                )
            
            # Execute appropriate step
            if step_type == "jira":
                # Pass CloudWatch response (not user query) for rich ticket description
                cloudwatch_response = workflow_item.get("cloudwatch_response", query)
                result = await workflow_graph.approve_workflow(workflow_id, query, account_name, step_results, cloudwatch_response=cloudwatch_response, session_id=session_id)
                save_step_result("jira", result)
                update_approval("kb_search")
                return {
                    "success": result.get("success", True),
                    "result": result.get("response", ""),
                    "next_approval": "kb_search",
                    "step_name": step_names.get(step_type, step_type),
                    "step_type": step_type
                }
                
            elif step_type == "kb_search":
                # Use CloudWatch response (not user query) for alarm extraction
                cloudwatch_response = workflow_item.get("cloudwatch_response", query)
                result = await workflow_graph.approve_kb_search(workflow_id, cloudwatch_response, account_name, step_results, session_id=session_id)
                save_step_result("kb_search", result)
                update_approval("remediation")
                return {
                    "success": result.get("success", True),
                    "result": result.get("response", ""),
                    "next_approval": "remediation",
                    "step_name": step_names.get(step_type, step_type),
                    "step_type": step_type
                }
                
            elif step_type == "remediation":
                # Use CloudWatch response (not user query) for alarm extraction
                cloudwatch_response = workflow_item.get("cloudwatch_response", query)
                result = await workflow_graph.approve_remediation(workflow_id, cloudwatch_response, account_name, step_results, use_dynamic=True, session_id=session_id)
                save_step_result("remediation", result)
                update_approval("verification")
                return {
                    "success": result.get("success", True),
                    "result": result.get("response", ""),
                    "execution_log": result.get("execution_log", []),
                    "next_approval": "verification",
                    "step_name": step_names.get(step_type, step_type),
                    "step_type": step_type
                }

            elif step_type == "verification":
                cloudwatch_response = workflow_item.get("cloudwatch_response", query)
                result = await workflow_graph.approve_verification(workflow_id, cloudwatch_response, account_name, step_results, session_id=session_id)
                save_step_result("verification", result)
                update_approval("closure")
                return {
                    "success": result.get("success", True),
                    "result": result.get("response", ""),
                    "alarm_state": result.get("alarm_state", "unknown"),
                    "verified": result.get("verified", False),
                    "next_approval": "closure",
                    "step_name": step_names.get(step_type, step_type),
                    "step_type": step_type
                }

            elif step_type == "closure":
                result = await workflow_graph.close_jira_ticket(workflow_id, query, account_name, step_results, session_id=session_id)
                save_step_result("closure", result)
                update_approval(None)  # Remove approval, workflow complete
                return {
                    "success": result.get("success", True),
                    "result": result.get("response", ""),
                    "workflow_complete": True,
                    "step_name": step_names.get(step_type, step_type),
                    "step_type": step_type
                }
            
            elif step_type == "full_auto":
                logger.info(f"Starting full automation for workflow {workflow_id}")
                update_approval(None)  # Remove approval immediately
                
                automation_result = await self.execute_full_automation(workflow_id, use_dynamic=True, progress_callback=progress_callback)
                
                step_results = []
                if automation_result.get("success") and automation_result.get("result"):
                    result_data = automation_result["result"]
                    for step_info in result_data.get("steps", []):
                        step_results.append({
                            "step_num": step_info.get("step_num", len(step_results) + 1),
                            "step_name": step_names.get(step_info.get("step"), step_info.get("step", "Unknown")),
                            "status": step_info.get("status"),
                            "result": step_info.get("result", "") if step_info.get("result") else "",
                            "message": step_info.get("message", "")
                        })
                
                return {
                    "success": automation_result.get("success", False),
                    "result": automation_result.get("message", "Full automation completed"),
                    "workflow_complete": True,
                    "step_name": "Full Automation",
                    "step_type": step_type,
                    "step_results": step_results
                }
            
            else:
                return {"success": False, "message": f"Unknown step type: {step_type}"}
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Step approval failed: {str(e)}"
            }
    
    async def reject_step(self, workflow_id: str, step_type: str) -> Dict:
        """
        Reject a workflow step (DynamoDB-backed).
        """
        try:
            # Remove from DynamoDB
            _table.delete_item(Key={"request_id": f"approval-{workflow_id}"})
            return {
                "success": True,
                "message": f"Workflow step '{step_type}' rejected"
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Step rejection failed: {str(e)}"
            }
    
    async def get_workflow_status(self, workflow_id: str) -> Dict:
        """
        Get current workflow status from DynamoDB.
        """
        try:
            workflow_response = _table.get_item(Key={"request_id": f"workflow-{workflow_id}"})
            workflow_item = workflow_response.get("Item")
            
            if not workflow_item:
                return {"success": False, "message": "Workflow not found"}
            
            workflow_item = _decimal_to_python(workflow_item)
            
            # Check for pending approval
            approval_response = _table.get_item(Key={"request_id": f"approval-{workflow_id}"})
            approval_item = approval_response.get("Item")
            pending_type = approval_item.get("approval_data", {}).get("type") if approval_item else None
            
            return {
                "success": True,
                "workflow_id": workflow_id,
                "created_at": workflow_item.get("created_at"),
                "has_alarm": workflow_item.get("has_alarm"),
                "pending_approval": pending_type,
                "automation_status": None  # Progress tracking not implemented in DynamoDB yet
            }
        except Exception as e:
            return {"success": False, "message": f"Status check failed: {str(e)}"}
    
    async def execute_full_automation(self, workflow_id: str, use_dynamic: bool = True, progress_callback=None) -> Dict:
        """
        Execute full automation for workflow (DynamoDB-backed).
        """
        try:
            # Get workflow from DynamoDB
            workflow_response = _table.get_item(Key={"request_id": f"workflow-{workflow_id}"})
            workflow_item = workflow_response.get("Item")
            
            if not workflow_item:
                return {"success": False, "message": "Workflow not found"}
            
            workflow_item = _decimal_to_python(workflow_item)
            query = workflow_item.get("query")
            cloudwatch_response = workflow_item.get("cloudwatch_response", query)
            account_name = workflow_item.get("account_name")
            session_id = workflow_item.get("session_id", f"workflow-{workflow_id}")

            # Recreate workflow graph with session
            customer_session = None
            if account_name and account_name != "default":
                self.workspace.set_current_account(account_name)
                customer_session = self.workspace.get_current_session()
            workflow_graph = CloudWatchJiraKBRemediationGraph(customer_session=customer_session)

            # Execute full automation (now async)
            result = await workflow_graph.execute_full_automation(
                workflow_id,
                query,
                cloudwatch_response,
                account_name,
                progress_callback=progress_callback,
                use_dynamic=use_dynamic,
                session_id=session_id
            )
            
            # Clear pending approval from DynamoDB
            _table.delete_item(Key={"request_id": f"approval-{workflow_id}"})
            
            return {
                "success": True,
                "result": result,
                "message": result.get("message", "Automation completed")
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Automation failed: {str(e)}"
            }
    
    async def get_automation_progress(self, workflow_id: str) -> Dict:
        """
        Get automation progress for workflow.
        Note: Progress tracking not yet implemented in DynamoDB.
        """
        return {"success": False, "message": "Progress tracking not available"}


# Singleton instance
_workflow_service = None

def get_workflow_service() -> WorkflowService:
    """Get singleton WorkflowService instance."""
    global _workflow_service
    if _workflow_service is None:
        _workflow_service = WorkflowService()
    return _workflow_service