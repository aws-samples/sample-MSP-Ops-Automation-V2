"""
AgentCore Stack: Memory, Identity, Policy, Gateway, Observability
=================================================================
Deployed FIRST — BackendStack and FrontendStack depend on ARNs exported here.

Architecture note:
  CDK L2 constructs for Amazon Bedrock AgentCore are not yet available, so all
  AgentCore resources (Memory, Gateway) are created imperatively via boto3 at
  CDK synthesis/deploy time rather than through CloudFormation.  This means:
    - Resources are created during `cdk deploy`, not as CloudFormation resources.
    - Re-running `cdk deploy` is idempotent: each helper uses a get-or-create
      pattern (list → find by name → create only if absent).
    - ARNs/IDs are stored in self.resources and passed to BackendStack via the
      agentcore_resources dict parameter.

Polling constants:
  POLL_INTERVAL_SECONDS / POLL_MAX_ATTEMPTS control how long to wait for Memory
  to reach ACTIVE status after creation (typically 1-3 minutes).
"""
from constructs import Construct
from aws_cdk import (
    Stack,
    aws_logs as logs,
    aws_iam as iam,
    CfnOutput, RemovalPolicy
)
import json
import boto3
import time
import logging

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
POLL_MAX_ATTEMPTS = 30


class AgentCoreStack(Stack):
    """
    Creates AgentCore resources via AWS CLI/SDK
    Note: CDK L2 constructs for AgentCore not yet available
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.resources = {}

        # Service-linked roles (Developer Guide p.1119-1126)
        # NOTE: These are account-level resources created once per account
        # They already exist in this account, so we skip creation
        # If needed in a new account, create them manually with:
        #   aws iam create-service-linked-role --aws-service-name network.bedrock-agentcore.amazonaws.com
        #   aws iam create-service-linked-role --aws-service-name runtime-identity.bedrock-agentcore.amazonaws.com

        # network_slr = iam.CfnServiceLinkedRole(
        #     self, "AgentCoreNetworkSLR",
        #     aws_service_name="network.bedrock-agentcore.amazonaws.com",
        #     description="Allows AgentCore to create ENIs for VPC connectivity"
        # )

        # identity_slr = iam.CfnServiceLinkedRole(
        #     self, "AgentCoreIdentitySLR",
        #     aws_service_name="runtime-identity.bedrock-agentcore.amazonaws.com",
        #     description="Manages workload identity tokens for OAuth"
        # )

        # 1. Enable Transaction Search (one-time account setup)
        self._enable_transaction_search()

        # 2. Create AgentCore Memory
        memory = self._create_memory()
        self.resources['memory_id'] = memory['id']
        self.resources['memory_arn'] = memory['arn']

        # 3. Create AgentCore Gateway
        gateway = self._create_gateway()
        self.resources['gateway_id'] = gateway['id']
        self.resources['gateway_arn'] = gateway['arn']
        self.resources['gateway_url'] = gateway['url']

        # 4. Enable AgentCore Managed Observability (Developer Guide p.844-850)
        self._enable_managed_observability(
            memory_id=memory['id'],
            memory_arn=memory['arn'],
            gateway_id=gateway['id'],
            gateway_arn=gateway['arn']
        )

        # 5. Supervisor Runtime ARN
        # Read from CDK context (passed by deploy.sh after Supervisor deployment)
        # Falls back to placeholder if not provided (for stack synthesis without deployment)
        supervisor_arn = self.node.try_get_context("supervisor_runtime_arn")
        self.resources['supervisor_runtime_arn'] = supervisor_arn or f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/supervisor-placeholder"

        if supervisor_arn:
            logger.info(f"Using Supervisor Runtime ARN from context: {supervisor_arn}")
        else:
            logger.warning(f"No supervisor_runtime_arn in context, using placeholder")

        # 6. A2A Specialist Runtime ARNs for direct routing
        # Read from CDK context (passed by deploy.sh Step 9 after A2A runtimes deployed)
        # Falls back to empty string if not provided (direct routing disabled, Supervisor used)
        a2a_arns = {
            'cloudwatch': self.node.try_get_context("cloudwatch_a2a_arn") or "",
            'security':   self.node.try_get_context("security_a2a_arn") or "",
            'cost':       self.node.try_get_context("cost_a2a_arn") or "",
            'advisor':    self.node.try_get_context("advisor_a2a_arn") or "",
            'jira':       self.node.try_get_context("jira_a2a_arn") or "",
            'knowledge':  self.node.try_get_context("knowledge_a2a_arn") or "",
        }

        self.resources['cloudwatch_a2a_arn'] = a2a_arns['cloudwatch']
        self.resources['security_a2a_arn']   = a2a_arns['security']
        self.resources['cost_a2a_arn']        = a2a_arns['cost']
        self.resources['advisor_a2a_arn']     = a2a_arns['advisor']
        self.resources['jira_a2a_arn']        = a2a_arns['jira']
        self.resources['knowledge_a2a_arn']   = a2a_arns['knowledge']

        configured_count = sum(1 for v in a2a_arns.values() if v)
        if configured_count > 0:
            logger.info(f"A2A direct routing ARNs loaded: {configured_count}/6 configured")
        else:
            logger.info(f"No A2A ARNs in context — direct routing disabled (Supervisor fallback active)")

        # Outputs
        CfnOutput(self, "MemoryId", value=self.resources['memory_id'])
        CfnOutput(self, "GatewayARN", value=self.resources['gateway_arn'])
        CfnOutput(self, "GatewayURL", value=self.resources['gateway_url'])
        CfnOutput(self, "SupervisorRuntimeARN", value=self.resources['supervisor_runtime_arn'])

    def _enable_transaction_search(self):
        """
        Enable CloudWatch Transaction Search
        Developer Guide p.829 - ALL 3 STEPS
        """
        logs_client = boto3.client('logs', region_name=self.region)
        xray_client = boto3.client('xray', region_name=self.region)

        try:
            # Step 1: Resource policy
            logs_client.put_resource_policy(
                policyName='TransactionSearchXRayAccess',
                policyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Sid": "TransactionSearchXRayAccess",
                        "Effect": "Allow",
                        "Principal": {"Service": "xray.amazonaws.com"},
                        "Action": "logs:PutLogEvents",
                        "Resource": [
                            f"arn:aws:logs:{self.region}:{self.account}:log-group:aws/spans:*",
                            f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/application-signals/data:*"
                        ],
                        "Condition": {
                            "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{self.region}:{self.account}:*"},
                            "StringEquals": {"aws:SourceAccount": self.account}
                        }
                    }]
                })
            )

            # Step 2: Trace destination
            try:
                xray_client.update_trace_segment_destination(
                    Destination='CloudWatchLogs'
                )
            except Exception as e:
                if "already set" in str(e).lower():
                    logger.info("Transaction Search destination already configured")
                else:
                    raise

            # Step 3: Indexing rule for searchability
            try:
                xray_client.update_indexing_rule(
                    Name="Default",
                    Rule={
                        'Probabilistic': {
                            'DesiredSamplingPercentage': 10.0
                        }
                    }
                )
            except Exception as e:
                if "validation" not in str(e).lower():
                    logger.warning(f"Indexing rule: {e}")

            logger.info("Transaction Search enabled (all 3 steps)")

        except Exception as e:
            if "AlreadyExistsException" not in str(e):
                logger.warning(f"Transaction Search: {e}")

    @staticmethod
    def _poll_memory_status(client, memory_id: str, memory_arn: str) -> dict:
        """Poll AgentCore memory until it reaches ACTIVE or FAILED status.

        AgentCore Memory provisioning is asynchronous — the API returns immediately
        after create_memory but the resource is not usable until status == 'ACTIVE'.
        Typical activation time is 60-180 seconds; POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS
        (300 s) provides a safe upper bound.

        Args:
            client: boto3 bedrock-agentcore-control client
            memory_id: ID returned by create_memory
            memory_arn: ARN returned by create_memory

        Returns:
            dict with 'id' and 'arn' keys once the memory is ACTIVE.

        Raises:
            Exception: if the memory reaches FAILED status.
            TimeoutError: if ACTIVE status is not reached within the polling budget.
        """
        for attempt in range(POLL_MAX_ATTEMPTS):
            status_response = client.get_memory(memoryId=memory_id)
            status = status_response['memory']['status']

            if status == 'ACTIVE':
                logger.info(f"Memory {memory_id} is ACTIVE")
                return {'id': memory_id, 'arn': memory_arn}
            elif status == 'FAILED':
                raise Exception(f"Memory FAILED: {status_response}")

            logger.info(f"  Memory status: {status}, waiting... ({attempt+1}/{POLL_MAX_ATTEMPTS})")
            # Intentional sleep: polling loop waiting for AgentCore Memory to reach ACTIVE status.
            # This runs at CDK synthesis time (not inside a Lambda or event loop), so blocking
            # sleep is acceptable here.
            time.sleep(POLL_INTERVAL_SECONDS)  # nosemgrep: arbitrary-sleep

        raise TimeoutError(f"Memory not ACTIVE after {POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s")

    def _create_memory(self) -> dict:
        """
        Create AgentCore Memory - Robust Get or Create pattern
        Uses GetMemory for each listed memory to find name match
        """
        client = boto3.client('bedrock-agentcore-control', region_name=self.region)
        memory_name = "msp_assistant_memory"

        # Step 1: List all memories (only returns id, arn, status - NO name)
        logger.info(f"Checking for existing memory: {memory_name}")
        try:
            memories_response = client.list_memories()

            # Step 2: GetMemory for each to check name (name only in full details)
            for mem_summary in memories_response.get('memories', []):
                try:
                    mem_detail = client.get_memory(memoryId=mem_summary['id'])
                    if mem_detail['memory'].get('name') == memory_name:
                        logger.info(f"Found existing memory: {mem_summary['id']}")
                        memory_id = mem_summary['id']
                        memory_arn = mem_summary['arn']

                        if mem_detail['memory']['status'] == 'ACTIVE':
                            logger.info(f"Memory {memory_id} is ACTIVE")
                            return {'id': memory_id, 'arn': memory_arn}

                        return self._poll_memory_status(client, memory_id, memory_arn)
                except (KeyError, client.exceptions.ResourceNotFoundException) as e:
                    logger.debug(f"Skipping memory {mem_summary.get('id')}: {e}")
                    continue
        except Exception as list_error:
            logger.warning(f"  Could not list memories: {list_error}")

        # Step 3: Create new memory if not found
        logger.info(f"Creating new memory: {memory_name}")
        try:
            response = client.create_memory(
                name=memory_name,
                description="Conversation and workflow memory",
                eventExpiryDuration=90,
                memoryStrategies=[
                    {
                        'semanticMemoryStrategy': {
                            'name': 'SemanticFacts',
                            'namespaces': ['/strategy/{memoryStrategyId}/actor/{actorId}/']
                        }
                    },
                    {
                        'summaryMemoryStrategy': {
                            'name': 'SessionSummaries',
                            'namespaces': ['/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/']
                        }
                    }
                ]
            )

            memory_id = response['memory']['id']
            memory_arn = response['memory']['arn']

        except Exception as create_error:
            if "already exists" in str(create_error):
                # Race condition - memory created between list and create
                logger.info(f"  Memory created concurrently, retrieving...")
                return self._create_memory()
            else:
                raise

        # Step 4: Wait for ACTIVE status
        return self._poll_memory_status(client, memory_id, memory_arn)

    def _find_gateway_by_name(self, client, gateway_name: str) -> dict | None:
        """Look up an existing gateway by name, returning its id/arn/url or None."""
        try:
            gateways_response = client.list_gateways()
            for gw in gateways_response.get('items', []):
                if gw.get('name') == gateway_name:
                    gw_detail = client.get_gateway(gatewayIdentifier=gw['gatewayId'])
                    return {
                        'id': gw['gatewayId'],
                        'arn': gw_detail['gatewayArn'],
                        'url': gw_detail['gatewayUrl']
                    }
        except Exception as e:
            logger.warning(f"Could not list gateways: {e}")
        return None

    def _create_gateway(self) -> dict:
        """
        Create AgentCore Gateway with dynamic role discovery
        Discovers IAM role based on current caller identity
        Skip-if-exists pattern to handle multiple CDK synthesis runs
        """
        client = boto3.client('bedrock-agentcore-control', region_name=self.region)
        sts = boto3.client('sts', region_name=self.region)

        # Get current caller identity for account ID
        identity = sts.get_caller_identity()

        # Use the standard gateway execution role created by deploy.sh
        role_arn = f"arn:aws:iam::{identity['Account']}:role/msp-gateway-execution-role"

        logger.info(f"Using gateway execution role: {role_arn}")

        gateway_name = "msp-assistant-gateway"

        # CHECK IF EXISTS FIRST - comprehensive search through all gateways
        logger.info(f"Checking for existing gateway: {gateway_name}")
        existing = self._find_gateway_by_name(client, gateway_name)
        if existing:
            logger.info(f"Using existing gateway: {existing['id']}")
            return existing

        # ONLY CREATE IF NOT FOUND
        logger.info(f"Creating new gateway: {gateway_name}")
        try:
            response = client.create_gateway(
                name=gateway_name,
                protocolType="MCP",
                authorizerType="AWS_IAM",
                roleArn=role_arn,
                protocolConfiguration={
                    'mcp': {
                        'searchType': 'SEMANTIC'
                    }
                }
            )

            logger.info(f"Created gateway: {response['gatewayArn']}")
            return {
                'id': response['gatewayId'],
                'arn': response['gatewayArn'],
                'url': response['gatewayUrl']
            }
        except client.exceptions.ConflictException:
            # Race condition: another CDK deploy (e.g. a parallel CI run or a rapid
            # re-deploy) created the gateway in the window between our list check above
            # and the create_gateway call.  ConflictException is the API's signal that
            # the resource name is already taken.  Retry the name lookup to obtain the
            # existing gateway's ID/ARN/URL rather than failing the deploy.
            logger.info(f"  Gateway created concurrently, retrieving...")
            existing = self._find_gateway_by_name(client, gateway_name)
            if existing:
                logger.info(f"Retrieved existing gateway: {existing['id']}")
                return existing

            # Should not reach here: ConflictException means the gateway exists, but
            # _find_gateway_by_name couldn't locate it (e.g. eventual-consistency lag).
            raise Exception(f"Gateway '{gateway_name}' exists but not retrievable. Check AWS console manually.")

    def _enable_managed_observability(self, memory_id: str, memory_arn: str, gateway_id: str, gateway_arn: str) -> None:
        """
        AgentCore Managed Observability.

        Note: tracingConfiguration is NOT a valid parameter for update_memory or update_gateway.
        Observability is automatically enabled by the agentcore CLI during `agentcore deploy`.
        The CLI creates CloudWatch Log groups and X-Ray trace destinations.

        This method is kept as a no-op placeholder for documentation purposes.
        """
        logger.info("AgentCore Managed Observability")
        logger.info("  Tracing is auto-configured by `agentcore deploy` CLI")
        logger.info(f"  Memory logs: /aws/bedrock-agentcore/memory/{memory_id}")
        logger.info(f"  Gateway logs: /aws/bedrock-agentcore/gateway/{gateway_id}")
        logger.info("AgentCore Managed Observability configured")
