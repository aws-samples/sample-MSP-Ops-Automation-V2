"""
AgentCore Type Definitions for Runtime Invocation
================================================

Defines payload structures for communication between ECS Backend and 
AgentCore Runtime container, enabling secure credential passing.

Security Pattern:
- ECS has Secrets Manager access → fetches customer credentials
- ECS includes credentials in Runtime payload (TLS 1.2+ encrypted)
- Runtime extracts credentials from payload (no Secrets Manager needed)
"""
from typing import Dict, Any, Optional
from pydantic import BaseModel


class CustomerCredentials(BaseModel):
    """
    Customer account credentials from Secrets Manager.
    
    These are STS temporary credentials with 60-minute lifetime.
    Passed from ECS backend to Runtime via encrypted payload.
    """
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str
    account_id: str
    account_name: str
    region: str = "us-east-1"


class RuntimeInvocationPayload(BaseModel):
    """
    Complete payload structure for Supervisor Runtime invocation.
    
    This payload is sent from ECS backend to AgentCore Runtime container.
    All fields are passed via TLS 1.2+ encrypted channel.
    """
    prompt: str
    account_name: str = "default"
    workflow_enabled: bool = False
    full_automation: bool = False
    session_id: str
    user_context: Dict[str, Any]
    customer_credentials: Optional[Dict[str, str]] = None  # None for MSP, populated for customer accounts


class RuntimeInvocationResponse(BaseModel):
    """
    Expected response structure from Supervisor Runtime.
    
    Runtime returns this structure after processing the request.
    """
    response: str
    agent_type: str
    session_id: str
    success: bool
    workflow_triggered: bool = False
    workflow_id: Optional[str] = None
    error: Optional[str] = None
