# app/core/config.py
"""
Configuration management for the FastAPI backend.
Loads environment variables and provides typed settings.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # AWS Cognito Configuration
    AWS_REGION: str = "us-east-1"
    COGNITO_USER_POOL_ID: str
    COGNITO_CLIENT_ID: str
    COGNITO_CLIENT_SECRET: Optional[str] = None  # Not needed for public clients, reserved
    
    # API Configuration
    API_VERSION: str = "v1"
    FRONTEND_URL: str = "http://localhost:5173"
    # Specific CloudFront distribution domain (e.g. d1abc123.cloudfront.net) — populated by deploy.sh.
    # Used to pin CORS regex to a single known distribution rather than allowing all of *.cloudfront.net.
    CLOUDFRONT_DOMAIN: Optional[str] = ""
    
    # Bedrock Model Configuration
    MODEL: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    AWS_PROFILE: Optional[str] = None
    BEDROCK_KNOWLEDGE_BASE_ID: Optional[str] = ""
    
    # Jira Configuration
    JIRA_URL: Optional[str] = ""
    JIRA_EMAIL: Optional[str] = ""
    JIRA_API_TOKEN: Optional[str] = ""
    JIRA_PROJECT_KEY: Optional[str] = ""
    UNRESOLVED_TICKET_EMAIL: Optional[str] = ""
    
    # Demo/Test Configuration
    API_ID: Optional[str] = ""
    TEST_ALARM_NAME: Optional[str] = ""
    
    # AgentCore Configuration (populated by deploy.sh)
    SUPERVISOR_RUNTIME_ARN: Optional[str] = ""
    GATEWAY_ARN: Optional[str] = ""
    GATEWAY_URL: Optional[str] = ""
    MEMORY_ID: Optional[str] = ""
    
    # A2A Specialist Runtime ARNs (populated by deploy.sh Step 9)
    # Used by DirectRouterClient to bypass Supervisor for known-domain queries
    CLOUDWATCH_A2A_ARN: Optional[str] = ""
    SECURITY_A2A_ARN: Optional[str] = ""
    COST_A2A_ARN: Optional[str] = ""
    ADVISOR_A2A_ARN: Optional[str] = ""
    JIRA_A2A_ARN: Optional[str] = ""
    KNOWLEDGE_A2A_ARN: Optional[str] = ""
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "allow"  # Allow extra fields from your current config


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
