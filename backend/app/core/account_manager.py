"""
Customer account lifecycle manager for cross-account MSP operations.

Architecture context:
  CrossAccountManager sits between the API layer and SecretsCredentialManager.
  Its responsibilities are:

  - Onboarding: validate a new customer account ID, generate a unique external_id
    and role name, produce a CloudFormation template and AWS CLI instructions the
    customer pastes into their own account to create the cross-account IAM role.

  - Session provision: given an account config dict, retrieve existing STS tokens
    from SecretsCredentialManager (Secrets Manager backed) or generate fresh ones
    via assume_role, then return a boto3 Session scoped to that customer.

  - Status & diagnostics: test live access, report credential health, list all
    managed accounts.

  - Offboarding: remove the account config and delete stored credentials from
    Secrets Manager.

  In-memory accounts dict:
    Account configurations (CustomerAccountConfig dataclasses) are held in
    self.accounts, an in-memory dict.  This is intentional for the current
    single-process deployment; a production scale-out would persist them in
    DynamoDB or Parameter Store.

  Singleton access: use get_cross_account_manager() to avoid redundant boto3
  session creation across the process lifetime.
"""
import boto3
import json
import logging
import uuid
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from app.core.secrets_credential_manager import (
    get_secrets_credential_manager,
    get_current_msp_account_id,
    get_current_msp_principal_arn,
    get_current_msp_principal_info
)

logger = logging.getLogger(__name__)

@dataclass
class CustomerAccountConfig:
    """Configuration for a customer account"""
    name: str
    id: str  # AWS Account ID
    role: str = None  # Cross-account role name
    external_id: str = None  # External ID for role assumption
    region: str = 'us-east-1'  # Default region
    description: str = None
    created_at: str = None
    last_accessed: str = None

class CrossAccountManager:
    """
    Manages customer account configurations and cross-account access.
    Integrates with SecretsCredentialManager for STS token management.
    """
    
    def __init__(self, aws_profile: str = None):
        """
        Initialize the Cross Account Manager
        
        Args:
            aws_profile: AWS profile to use for MSP operations
        """
        try:
            if aws_profile:
                self.session = boto3.Session(profile_name=aws_profile)
            else:
                self.session = boto3.Session()
            
            # Get MSP account ID dynamically
            self.msp_account_id = get_current_msp_account_id()
            
            # Initialize secrets manager for credential operations
            self.secrets_manager = get_secrets_credential_manager(aws_profile)
            
            # In-memory storage for account configurations
            # In production, this could be stored in DynamoDB or Parameter Store
            self.accounts: Dict[str, CustomerAccountConfig] = {}
            
            logger.info(f"CrossAccountManager initialized for MSP account: {self.msp_account_id}")
            
        except Exception as e:
            raise RuntimeError(f"Failed to initialize CrossAccountManager: {e}")
    
    def add_account(self, customer_name: str, customer_account_id: str, 
                   description: str = None) -> Tuple[bool, str, Dict]:
        """
        Add a new customer account with auto-generated cross-account role configuration
        
        Args:
            customer_name: Display name for the customer
            customer_account_id: Customer's AWS account ID
            description: Optional description
            
        Returns:
            Tuple of (success, message, role_config)
        """
        try:
            # Validate inputs
            if not customer_name or not customer_account_id:
                return False, "Customer name and account ID are required", {}
            
            if len(customer_account_id) != 12 or not customer_account_id.isdigit():
                return False, "Invalid AWS account ID format", {}
            
            # Check if account already exists
            if customer_name in self.accounts:
                return False, f"Account '{customer_name}' already exists", {}
            
            # Generate unique identifiers (customer_name already sanitized from frontend)
            external_id = f"msp-{customer_name}-{str(uuid.uuid4())[:8]}"
            role_name = f"MSP-{customer_name}-Role"
            
            # Create account configuration
            account_config = CustomerAccountConfig(
                name=customer_name,
                id=customer_account_id,
                role=role_name,
                external_id=external_id,
                description=description,
                created_at=boto3.Session().client('sts').get_caller_identity().get('Arn', 'unknown')
            )
            
            # Store account configuration
            self.accounts[customer_name] = account_config
            
            # Generate CloudFormation template for customer
            role_template = self._generate_cross_account_role_template(
                self.msp_account_id, role_name, external_id
            )
            
            logger.info(f"Added customer account: {customer_name} ({customer_account_id})")
            
            return True, f"Account '{customer_name}' added successfully", {
                "account_config": asdict(account_config),
                "cloudformation_template": role_template,
                "setup_instructions": self._generate_setup_instructions(
                    customer_name, customer_account_id, role_name, external_id
                )
            }
            
        except Exception as e:
            return False, f"Failed to add account: {str(e)}", {}
    
    def get_session(self, account_config: Dict[str, Any]) -> Tuple[Optional[boto3.Session], Optional[str]]:
        """
        Get a boto3 session for the customer account using STS tokens from Secrets Manager
        
        Args:
            account_config: Customer account configuration
            
        Returns:
            Tuple of (session, error_message)
        """
        try:
            account_name = account_config['name']
            
            # Try to get existing credentials from Secrets Manager
            credentials_dict = self.secrets_manager.get_customer_credentials(account_name)
            
            if credentials_dict:
                # Create session with stored credentials
                session = boto3.Session(
                    aws_access_key_id=credentials_dict['aws_access_key_id'],
                    aws_secret_access_key=credentials_dict['aws_secret_access_key'],
                    aws_session_token=credentials_dict['aws_session_token']
                )
                return session, None
            
            # No valid credentials found, try to generate new ones
            logger.info(f"Generating new STS tokens for {account_name}")
            
            success = self.secrets_manager.generate_and_store_tokens(
                account_name,
                account_config.get('id', account_config.get('account_id')),
                account_config.get('role'),
                account_config.get('external_id')
            )
            
            if success:
                # Retry getting credentials after generation
                credentials_dict = self.secrets_manager.get_customer_credentials(account_name)
                if credentials_dict:
                    session = boto3.Session(
                        aws_access_key_id=credentials_dict['aws_access_key_id'],
                        aws_secret_access_key=credentials_dict['aws_secret_access_key'],
                        aws_session_token=credentials_dict['aws_session_token']
                    )
                    return session, None
            
            return None, "Failed to obtain customer credentials"
            
        except Exception as e:
            return None, f"Session creation failed: {str(e)}"
    
    def test_account_access(self, account_name: str) -> Tuple[bool, str]:
        """
        Test access to a customer account
        
        Args:
            account_name: Customer account name
            
        Returns:
            Tuple of (success, message)
        """
        try:
            account_config = self.get_account_by_name(account_name)
            if not account_config:
                return False, f"Account '{account_name}' not found"
            
            # Test using secrets manager
            return self.secrets_manager.test_customer_access(account_name)
            
        except Exception as e:
            return False, f"Access test failed: {str(e)}"
    
    def remove_account(self, account_name: str) -> Tuple[bool, str]:
        """
        Remove a customer account and clean up stored credentials
        
        Args:
            account_name: Customer account name
            
        Returns:
            Tuple of (success, message)
        """
        try:
            if account_name not in self.accounts:
                return False, f"Account '{account_name}' not found"
            
            # Delete stored credentials
            self.secrets_manager.delete_customer_credentials(account_name)
            
            # Remove from local configuration
            del self.accounts[account_name]
            
            logger.info(f"Removed customer account: {account_name}")
            return True, f"Account '{account_name}' removed successfully"
            
        except Exception as e:
            return False, f"Failed to remove account: {str(e)}"
    
    def get_account_by_name(self, account_name: str) -> Optional[Dict[str, Any]]:
        """
        Get account configuration by name
        
        Args:
            account_name: Customer account name
            
        Returns:
            Account configuration dictionary or None
        """
        if account_name in self.accounts:
            return asdict(self.accounts[account_name])
        return None
    
    def list_accounts(self) -> List[Dict[str, Any]]:
        """
        List all configured customer accounts
        
        Returns:
            List of account configuration dictionaries
        """
        return [asdict(config) for config in self.accounts.values()]
    
    def get_account_status(self, account_name: str) -> Dict[str, Any]:
        """
        Get comprehensive status for a customer account
        
        Args:
            account_name: Customer account name
            
        Returns:
            Status dictionary with access, credentials, and configuration info
        """
        try:
            account_config = self.get_account_by_name(account_name)
            if not account_config:
                return {"status": "not_found", "message": "Account not configured"}
            
            # Test access
            access_success, access_message = self.test_account_access(account_name)
            
            # Check credentials
            credentials = self.secrets_manager.get_customer_credentials(account_name)
            has_credentials = credentials is not None
            
            return {
                "status": "active" if access_success else "error",
                "account_name": account_name,
                "account_id": account_config.get('id'),
                "has_credentials": has_credentials,
                "access_test": {
                    "success": access_success,
                    "message": access_message
                },
                "role_arn": f"arn:aws:iam::{account_config.get('id')}:role/{account_config.get('role')}",
                "last_checked": boto3.Session().client('sts').get_caller_identity().get('Arn', 'unknown')
            }
            
        except Exception as e:
            return {
                "status": "error",
                "account_name": account_name,
                "message": f"Status check failed: {str(e)}"
            }
    
    def _build_cfn_template(self, role_name: str, external_id: str, principal_arn: str, description: str = "") -> Dict:
        """Build a CloudFormation template dict for the cross-account IAM role."""
        return {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": description or f"Cross-account role for MSP access",
            "Resources": {
                "CrossAccountRole": {
                    "Type": "AWS::IAM::Role",
                    "Properties": {
                        "RoleName": role_name,
                        "AssumeRolePolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [{
                                "Effect": "Allow",
                                "Principal": {"AWS": principal_arn},
                                "Action": "sts:AssumeRole",
                                "Condition": {
                                    "StringEquals": {"sts:ExternalId": external_id}
                                }
                            }]
                        },
                        "Policies": [{
                            "PolicyName": "MSPCrossAccountPolicy",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Sid": "CloudWatchReadOnly",
                                        "Effect": "Allow",
                                        "Action": [
                                            "cloudwatch:DescribeAlarms",
                                            "cloudwatch:DescribeAlarmsForMetric",
                                            "cloudwatch:GetMetricData",
                                            "cloudwatch:GetMetricStatistics",
                                            "cloudwatch:ListMetrics",
                                            "cloudwatch:GetDashboard",
                                            "cloudwatch:ListDashboards"
                                        ],
                                        "Resource": "*"
                                    },
                                    {
                                        "Sid": "CloudWatchLogsReadOnly",
                                        "Effect": "Allow",
                                        "Action": [
                                            "logs:DescribeLogGroups",
                                            "logs:DescribeLogStreams",
                                            "logs:GetLogEvents",
                                            "logs:FilterLogEvents",
                                            "logs:GetLogGroupFields"
                                        ],
                                        "Resource": "*"
                                    },
                                    {
                                        "Sid": "SecurityHubAccess",
                                        "Effect": "Allow",
                                        "Action": [
                                            "securityhub:GetFindings",
                                            "securityhub:BatchGetSecurityControls",
                                            "securityhub:GetEnabledStandards",
                                            "securityhub:DescribeStandards",
                                            "securityhub:DescribeStandardsControls",
                                            "securityhub:BatchUpdateFindings"
                                        ],
                                        "Resource": "*"
                                    },
                                    {
                                        "Sid": "CostExplorerReadOnly",
                                        "Effect": "Allow",
                                        "Action": [
                                            "ce:GetCostAndUsage",
                                            "ce:GetCostForecast",
                                            "ce:GetReservationUtilization",
                                            "ce:GetSavingsPlansUtilization",
                                            "ce:GetCostCategories",
                                            "ce:GetDimensionValues",
                                            "ce:GetRightsizingRecommendation",
                                            "ce:GetSavingsPlansPurchaseRecommendation",
                                            "ce:GetReservationPurchaseRecommendation",
                                            "ce:GetAnomalies"
                                        ],
                                        "Resource": "*"
                                    },
                                    {
                                        "Sid": "TrustedAdvisorReadOnly",
                                        "Effect": "Allow",
                                        "Action": [
                                            "support:DescribeTrustedAdvisorChecks",
                                            "support:DescribeTrustedAdvisorCheckResult",
                                            "support:DescribeTrustedAdvisorCheckSummaries",
                                            "support:RefreshTrustedAdvisorCheck"
                                        ],
                                        "Resource": "*"
                                    },
                                    {
                                        "Sid": "APIGatewayRemediation",
                                        "Effect": "Allow",
                                        "Action": [
                                            "apigateway:GET",
                                            "apigateway:PATCH",
                                            "apigateway:POST",
                                            "apigateway:PUT",
                                            "apigateway:UpdateRestApiPolicy"
                                        ],
                                        "Resource": [
                                            "arn:aws:apigateway:*::/restapis",
                                            "arn:aws:apigateway:*::/restapis/*"
                                        ]
                                    },
                                    {
                                        "Sid": "STSGetCallerIdentity",
                                        "Effect": "Allow",
                                        "Action": "sts:GetCallerIdentity",
                                        "Resource": "*"
                                    }
                                ]
                            }
                        }]
                    }
                }
            },
            "Outputs": {
                "RoleArn": {
                    "Description": "ARN of the created cross-account role",
                    "Value": {"Fn::GetAtt": ["CrossAccountRole", "Arn"]}
                },
                "RoleName": {
                    "Description": "Name of the created role",
                    "Value": {"Ref": "CrossAccountRole"}
                }
            }
        }

    def _generate_cross_account_role_template(self, msp_account_id: str,
                                              role_name: str, external_id: str) -> Dict:
        """Generate CloudFormation template for cross-account role using dynamic MSP principal."""
        try:
            msp_principal_arn = get_current_msp_principal_arn()
            template = self._build_cfn_template(
                role_name, external_id, msp_principal_arn,
                description=f"Cross-account role for MSP access from {msp_principal_arn}"
            )
            # Add Parameters section for the dynamic-principal variant
            template["Parameters"] = {
                "MSPPrincipalArn": {
                    "Type": "String",
                    "Default": msp_principal_arn,
                    "Description": "MSP user/role ARN that will assume this role"
                },
                "ExternalId": {
                    "Type": "String",
                    "Default": external_id,
                    "Description": "External ID for additional security"
                }
            }
            return template
        except Exception as e:
            logger.error(f"Error generating CloudFormation template: {e}")
            # Fallback: use root account principal
            return self._build_cfn_template(
                role_name, external_id,
                f"arn:aws:iam::{msp_account_id}:root",
                description=f"Cross-account role for MSP access from account {msp_account_id}"
            )

    def _generate_setup_instructions(self, customer_name: str, customer_account_id: str,
                                   role_name: str, external_id: str) -> Dict[str, Any]:
        """
        Generate setup instructions for the customer using dynamic MSP principal
        
        Returns:
            Dictionary with setup instructions and templates
        """
        try:
            # Get current MSP principal information
            msp_info = get_current_msp_principal_info()
            msp_principal_arn = msp_info['arn']
            
            # Create inline trust policy for CLI commands
            trust_policy_json = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"AWS": msp_principal_arn},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"sts:ExternalId": external_id}
                    }
                }]
            })
            
            return {
                "customer_name": customer_name,
                "customer_account_id": customer_account_id,
                "msp_account_id": self.msp_account_id,
                "msp_principal_arn": msp_principal_arn,
                "role_name": role_name,
                "external_id": external_id,
                "aws_cli_commands": [
                    f"# Create cross-account role with MSP principal",
                    f"aws iam create-role --role-name {role_name} --assume-role-policy-document '{trust_policy_json}' --description 'Cross-account access for MSP monitoring'",
                    f"",
                    f"# Attach least-privilege managed policies for monitoring",
                    f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
                    f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/AWSSecurityHubReadOnlyAccess",
                    f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/AWSCostAndUsageReportAutomationPolicy",
                    f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/AWSSupportAccess",
                    f"",
                    f"# For API Gateway remediation, create inline policy (optional - only if remediation needed)",
                    f"# aws iam put-role-policy --role-name {role_name} --policy-name APIGatewayRemediation --policy-document file://apigateway-policy.json",
                    f"",
                    f"# Verify role creation",
                    f"aws iam get-role --role-name {role_name}"
                ],
                "role_arn": f"arn:aws:iam::{customer_account_id}:role/{role_name}",
                "test_command": f"aws sts assume-role --role-arn arn:aws:iam::{customer_account_id}:role/{role_name} --role-session-name test-session --external-id {external_id}"
            }
        except Exception as e:
            logger.error(f"Error generating setup instructions: {e}")
            # Fallback to basic instructions
            return {
                "customer_name": customer_name,
                "customer_account_id": customer_account_id,
                "msp_account_id": self.msp_account_id,
                "role_name": role_name,
                "external_id": external_id,
                "error": f"Could not generate dynamic instructions: {str(e)}"
            }


# Cached singleton — avoids creating new boto3 sessions on every request.
_cross_account_manager_instance: "CrossAccountManager | None" = None


def get_cross_account_manager(aws_profile: str = None) -> "CrossAccountManager":
    """Return a cached CrossAccountManager singleton."""
    global _cross_account_manager_instance
    if _cross_account_manager_instance is None:
        _cross_account_manager_instance = CrossAccountManager(aws_profile)
    return _cross_account_manager_instance
