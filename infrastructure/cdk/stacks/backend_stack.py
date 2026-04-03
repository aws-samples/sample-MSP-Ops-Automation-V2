"""
Backend Stack: ECS Fargate + ALB + API Gateway + Cognito
=========================================================
Provisions the compute and API surface for the MSP Ops Automation platform.

Key design decisions and architecture context:

  AlbToFargate (AWS Solutions Construct)
    Creates a VPC, ECS Cluster, Fargate Service, and public ALB in one call.
    VPC is pinned to 2 AZs (max_azs=2) to prevent subnet CIDR drift on updates.
    The ALB is PUBLIC because API Gateway VPC Link integration requires an NLB;
    an ALB cannot be the integration target for a REST API VPC Link.

  API Gateway (HTTP_PROXY integration over public ALB)
    All routes are forwarded to the ALB via HTTP_PROXY.  Cognito authorizer
    gates every non-auth, non-health route.  OPTIONS methods are left
    unauthenticated on every resource so browser preflight requests succeed.

  Auth routes (/api/v1/auth/restore, /api/v1/auth/set-refresh, /api/v1/auth/logout)
    Must be unauthenticated because they are called either before a token exists
    or as part of the token-refresh flow.  They are defined as explicit resources
    so they take precedence over the {proxy+} catch-all at the same path level.

  CORS / Gateway Responses
    API Gateway does not add CORS headers to 4XX/5XX error responses by default.
    Without explicit GatewayResponse overrides, browsers report 401/403 as
    generic CORS errors, masking the real auth failure.  We attach CORS headers
    to UNAUTHORIZED, ACCESS_DENIED, DEFAULT_4XX, and DEFAULT_5XX responses.
    On first deploy FRONTEND_URL is empty (CloudFront not yet created), so
    localhost is used.  deploy.sh Step 11 updates the Gateway Responses via CLI
    once the CloudFront URL is known.

  DynamoDB (msp-assistant-chat-requests)
    Stores async chat request state so any Fargate task replica can poll and
    serve a long-running workflow result.  TTL auto-expires items after 10 min.

  Cognito
    Self-sign-up is disabled — MSP operators are added manually.
    An M2M client (client_credentials flow) is provided for Gateway → MCP
    server OAuth authentication.

Dependencies:
  Receives agentcore_resources dict from AgentCoreStack (ARNs baked into ECS
  environment variables at synthesis time).
  Exports self.api_url and self.cognito_config for FrontendStack.
"""
import os
from constructs import Construct
from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_logs as logs,
    aws_cognito as cognito,
    aws_apigateway as apigw,
    aws_elasticloadbalancingv2 as elbv2,
    aws_dynamodb as dynamodb,
    CfnOutput, Duration, RemovalPolicy
)
from aws_solutions_constructs.aws_alb_fargate import AlbToFargate

class BackendStack(Stack):
    """
    Backend infrastructure using AWS Solutions Constructs patterns
    Validated: aws-alb-fargate, aws-cloudfront-apigateway
    """
    
    def __init__(self, scope: Construct, id: str, agentcore_resources: dict, **kwargs):
        super().__init__(scope, id, **kwargs)
        
        # 1. ECR Repository for backend image
        # Import existing repository created by deploy.sh (not creating to avoid conflicts)
        ecr_repo = ecr.Repository.from_repository_name(
            self, "BackendRepository",
            repository_name="msp-assistant-backend"
        )
        
        # 2. DynamoDB Table for async chat state (multi-container support)
        chat_table = dynamodb.Table(self, "ChatRequestsTable",
            table_name="msp-assistant-chat-requests",
            partition_key=dynamodb.Attribute(name="request_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,  # Serverless, scales to zero
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl",  # Auto-cleanup expired items after 10 minutes
        )
        
        # 3. ECS Task Role (invoke AgentCore) - Complete IAM permissions
        task_role = iam.Role(self, "BackendTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Role for ECS tasks to invoke AgentCore services"
        )
        
        # Grant DynamoDB access for async chat state
        chat_table.grant_read_write_data(task_role)
        
        # AgentCore permissions (thin orchestration layer)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                "bedrock-agentcore:StopRuntimeSession",
                "bedrock-agentcore:InvokeGateway",
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:DeleteEvent",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:ListMemoryRecords",
                "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            ],
            resources=["*"]
        ))

        # AgentCore control plane (list/describe runtimes)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock-agentcore:ListAgentRuntimes",
                "bedrock-agentcore:GetAgentRuntime",
            ],
            resources=["*"]
        ))

        # Bedrock model invocation (health summary generation)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{self.region}::foundation-model/*"]
        ))
        
        # Bedrock Knowledge Base retrieval (workflow KB search)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:Retrieve"],
            resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"]
        ))

        # AWS Health API (outage dashboard)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "health:DescribeEvents",
                "health:DescribeEventDetails",
                "health:DescribeEventAggregates",
            ],
            resources=["*"]
        ))

        # STS (cross-account assume role, caller identity)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["sts:GetCallerIdentity", "sts:AssumeRole"],
            resources=["*"]
        ))

        # Secrets Manager (customer account credentials under msp-credentials/ prefix)
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:CreateSecret", "secretsmanager:GetSecretValue",
                     "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret",
                     "secretsmanager:DescribeSecret", "secretsmanager:UpdateSecret"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:msp-credentials/*"]
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:ListSecrets"],
            resources=["*"]
        ))
        
        # 4. Cognito User Pool (create before ECS so we can pass IDs to container)
        user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="msp-assistant-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True
            ),
            mfa=cognito.Mfa.OPTIONAL,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY
        )
        
        user_pool_client = user_pool.add_client("WebClient",
            auth_flows=cognito.AuthFlow(
                user_password=True,  # Allow plaintext auth (fallback)
                user_srp=True        # Allow SRP auth (secure, SDK default)
            ),
            generate_secret=False,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30)
        )
        
        # M2M OAuth: Resource server + client for Gateway→MCP authentication
        resource_server = user_pool.add_resource_server("MCPResourceServer",
            identifier="mcp-server",
            scopes=[cognito.ResourceServerScope(
                scope_name="invoke",
                scope_description="Invoke MCP server tools"
            )]
        )
        
        m2m_client = user_pool.add_client("M2MClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.custom("mcp-server/invoke")]
            ),
            access_token_validity=Duration.hours(1)
        )
        # Ensure resource server exists before M2M client
        m2m_client.node.add_dependency(resource_server)
        
        # Add Cognito domain for OAuth token endpoint
        user_pool_domain = user_pool.add_domain("CognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"msp-assistant-{self.account}"
            )
        )
        
        # 5. ECS Fargate + ALB (AWS Solutions Construct)
        # Note: AlbToFargate creates its own VPC automatically
        # Using public ALB for testing (API Gateway VPC Link requires NLB, not ALB)
        # IMPORTANT: This call must remain IDENTICAL across both deployment stages
        # CloudFormation will skip updating unchanged resources (VPC, ALB, ECS)
        alb_fargate = AlbToFargate(self, "BackendService",
            public_api=True,  # Public ALB (VPC Link only works with NLB)
            vpc_props=ec2.VpcProps(max_azs=2),  # Pin to 2 AZs — prevents subnet CIDR drift on updates
            container_definition_props=ecs.ContainerDefinitionOptions(
                image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "latest"),
                port_mappings=[ecs.PortMapping(container_port=8000)],  # CRITICAL: Match Dockerfile EXPOSE
                # Backend is thin orchestration layer - passes config to Runtime via ECS env vars
                environment={
                    # AWS Configuration
                    "AWS_REGION": self.region,
                    
                    # Cognito Configuration (required by backend)
                    "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                    "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                    
                    # AgentCore ARNs (Runtime handles observability)
                    "SUPERVISOR_RUNTIME_ARN": agentcore_resources['supervisor_runtime_arn'],
                    "GATEWAY_ARN": agentcore_resources['gateway_arn'],
                    "GATEWAY_URL": agentcore_resources['gateway_url'],
                    "MEMORY_ID": agentcore_resources['memory_id'],
                    
                    # DynamoDB for async chat state
                    "CHAT_REQUESTS_TABLE": chat_table.table_name,
                    
                    # Bedrock Configuration (for Runtime agents)
                    "MODEL": os.getenv('MODEL', 'global.anthropic.claude-sonnet-4-20250514-v1:0'),
                    "BEDROCK_KNOWLEDGE_BASE_ID": os.getenv('BEDROCK_KNOWLEDGE_BASE_ID', ''),
                    
                    # Agent Prompts (for Runtime agents) - multi-line strings from config_loader.py
                    "CLOUDWATCH_PROMPT": os.getenv('CLOUDWATCH_PROMPT', 'You are an expert AWS CloudWatch assistant.'),
                    "JIRA_PROMPT": os.getenv('JIRA_PROMPT', 'You are an expert Jira assistant.'),
                    "KNOWLEDGEBASE_PROMPT": os.getenv('KNOWLEDGEBASE_PROMPT', 'You are a fast API Gateway troubleshooting specialist.'),
                    
                    # Jira Configuration (for Runtime agents)
                    "JIRA_URL": os.getenv('JIRA_DOMAIN', ''),
                    "JIRA_DOMAIN": os.getenv('JIRA_DOMAIN', ''),
                    "JIRA_EMAIL": os.getenv('JIRA_EMAIL', ''),
                    "JIRA_API_TOKEN": os.getenv('JIRA_API_TOKEN', ''),
                    "JIRA_PROJECT_KEY": os.getenv('JIRA_PROJECT_KEY', ''),
                    
                    # A2A Specialist Runtime ARNs for direct routing (bypasses Supervisor LLM hop)
                    # Populated by deploy.sh Step 9 via agentcore_resources dict
                    "CLOUDWATCH_A2A_ARN": agentcore_resources.get('cloudwatch_a2a_arn', ''),
                    "SECURITY_A2A_ARN": agentcore_resources.get('security_a2a_arn', ''),
                    "COST_A2A_ARN": agentcore_resources.get('cost_a2a_arn', ''),
                    "ADVISOR_A2A_ARN": agentcore_resources.get('advisor_a2a_arn', ''),
                    "JIRA_A2A_ARN": agentcore_resources.get('jira_a2a_arn', ''),
                    "KNOWLEDGE_A2A_ARN": agentcore_resources.get('knowledge_a2a_arn', ''),
                },
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix="backend",
                    log_retention=logs.RetentionDays.ONE_WEEK
                )
            ),
            fargate_task_definition_props=ecs.FargateTaskDefinitionProps(
                task_role=task_role,
                cpu=512,  # Task CPU must be >= sum of container CPUs
                memory_limit_mib=1024  # Task memory must be >= sum of container memory
            ),
            fargate_service_props={
                "desired_count": 2,
                # min_healthy_percent=50 allows rolling deploys to take one task down
                # while the replacement starts (no over-provisioning needed).
                # max_healthy_percent=200 allows ECS to start replacement tasks before
                # stopping the old ones, achieving zero-downtime rolling updates.
                "min_healthy_percent": 50,
                "max_healthy_percent": 200,
                # Grace period gives the container time to start and pass health checks
                # before the ALB begins routing traffic and ECS begins evaluating health.
                "health_check_grace_period": Duration.seconds(60)
            },
            listener_props={
                "port": 80,
                "protocol": elbv2.ApplicationProtocol.HTTP
            },
            target_group_props={
                "health_check": elbv2.HealthCheck(
                    path="/health",
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(10),
                    healthy_threshold_count=2,
                    unhealthy_threshold_count=3
                ),
                "deregistration_delay": Duration.seconds(30)
            }
        )

        # ECS auto-scaling: scale 2→10 tasks on CPU utilization
        scaling = alb_fargate.service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=10,
        )
        scaling.scale_on_cpu_utilization("CpuScaling",
            target_utilization_percent=70,
            # Asymmetric cooldowns: scale out quickly (60s) to absorb traffic spikes,
            # but scale in slowly (300s) to avoid thrashing during bursty workloads.
            scale_in_cooldown=Duration.seconds(300),
            scale_out_cooldown=Duration.seconds(60),
        )

        # 6. API Gateway with Cognito Authorizer
        # CORS: include localhost for dev + CloudFront URL if already known from a prior deployment.
        # On first deploy FRONTEND_URL is empty; deploy.sh step 11 updates Gateway Responses via AWS CLI.
        # On redeployments (e.g. step 9.8) FRONTEND_URL is set from backend/.env so CDK bakes it in
        # directly, preventing every CDK redeploy from resetting ACAO headers back to localhost.
        frontend_url = os.getenv('FRONTEND_URL', '')
        allowed_origins = [
            "http://localhost:5173",  # Vite dev server
            "http://localhost:3000",  # Alternative dev server
        ]
        if frontend_url and frontend_url.startswith('https://') and frontend_url not in allowed_origins:
            allowed_origins.append(frontend_url)
        
        api = apigw.RestApi(self, "BackendAPI",
            rest_api_name="msp-assistant-api",
            description="MSP Assistant API with Cognito auth",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=allowed_origins,
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["Content-Type", "Authorization", "X-Amzn-Trace-Id", "Cache-Control"],
                allow_credentials=True,  # Required for Cognito authentication
                max_age=Duration.seconds(600)
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=200,
                logging_level=apigw.MethodLoggingLevel.INFO,
                metrics_enabled=True
            )
        )
        
        # Cognito authorizer
        auth = apigw.CognitoUserPoolsAuthorizer(self, "Authorizer",
            cognito_user_pools=[user_pool]
        )
        
        # Integration for REGULAR resources (health endpoint - no proxy parameter)
        health_integration = apigw.Integration(
            type=apigw.IntegrationType.HTTP_PROXY,
            integration_http_method="ANY",
            uri=f"http://{alb_fargate.load_balancer.load_balancer_dns_name}/health",
            options=apigw.IntegrationOptions(
                timeout=Duration.seconds(29)
            )
        )
        
        # Integration for PROXY resources (with proxy path parameter)
        proxy_integration = apigw.Integration(
            type=apigw.IntegrationType.HTTP_PROXY,
            integration_http_method="ANY",
            uri=f"http://{alb_fargate.load_balancer.load_balancer_dns_name}/{{proxy}}",
            options=apigw.IntegrationOptions(
                timeout=Duration.seconds(29),
                request_parameters={
                    "integration.request.path.proxy": "method.request.path.proxy"
                }
            )
        )
        
        # Explicit /health endpoint without authentication (required for ALB health checks)
        health_resource = api.root.add_resource('health')
        health_resource.add_method('GET', health_integration,
            authorization_type=apigw.AuthorizationType.NONE
        )
        
        # Routes - add authenticated methods and unauthenticated OPTIONS separately
        # NOTE: Do NOT add ANY method to root as it would require auth for OPTIONS (preflight)
        # OPTIONS requests never include credentials, so they must not require authentication

        # --- Explicit unauthenticated auth routes ---
        # These must be explicit resources (not caught by {proxy+}) because auth/restore
        # is called on page load WITHOUT a token — it IS the mechanism to obtain tokens.
        # Explicit resources in API GW take precedence over {proxy+} at the same level.
        #
        # IMPORTANT: Adding explicit /api resource "steals" the /api/* prefix from the
        # root {proxy+}. We therefore also add a {proxy+} under /api/v1 with Cognito auth
        # to handle all other /api/v1/* routes (chat, accounts, workflows, etc.).
        alb_dns = alb_fargate.load_balancer.load_balancer_dns_name

        def _fixed_auth_integration(path: str) -> apigw.Integration:
            """Fixed-path integration for auth endpoints (no {proxy} variable)."""
            return apigw.Integration(
                type=apigw.IntegrationType.HTTP_PROXY,
                integration_http_method="POST",
                uri=f"http://{alb_dns}/{path}",
                options=apigw.IntegrationOptions(timeout=Duration.seconds(29))
            )

        api_r  = api.root.add_resource("api")
        v1_r   = api_r.add_resource("v1")
        auth_r = v1_r.add_resource("auth")

        for auth_endpoint in ["restore", "set-refresh", "logout"]:
            auth_sub = auth_r.add_resource(auth_endpoint)
            auth_sub.add_method(
                "POST",
                _fixed_auth_integration(f"api/v1/auth/{auth_endpoint}"),
                authorization_type=apigw.AuthorizationType.NONE,
                method_responses=[apigw.MethodResponse(status_code="200")]
            )

        # --- Authenticated catch-all for /api/v1/* ---
        # Handles chat, accounts, workflows, health-checks etc.
        # URI must include /api/v1/ prefix so the forwarded path is correct.
        v1_proxy_integration = apigw.Integration(
            type=apigw.IntegrationType.HTTP_PROXY,
            integration_http_method="ANY",
            uri=f"http://{alb_dns}/api/v1/{{proxy}}",
            options=apigw.IntegrationOptions(
                timeout=Duration.seconds(29),
                request_parameters={
                    "integration.request.path.proxy": "method.request.path.proxy"
                }
            )
        )

        v1_proxy = v1_r.add_proxy(
            default_integration=v1_proxy_integration,
            # any_method=False: do NOT add an ANY method here.  ANY would apply the
            # Cognito authorizer to OPTIONS preflight requests, which browsers send
            # without credentials — causing every cross-origin call to fail with 401
            # before the real request is attempted.  Individual HTTP methods with auth
            # are added explicitly in the loop below; OPTIONS is left absent (handled
            # by the default CORS preflight options on the RestApi).
            any_method=False
        )

        _method_responses = [
            apigw.MethodResponse(
                status_code="200",
                response_parameters={
                    "method.response.header.Access-Control-Allow-Origin": True,
                    "method.response.header.Access-Control-Allow-Headers": True,
                    "method.response.header.Access-Control-Allow-Methods": True,
                    "method.response.header.Access-Control-Allow-Credentials": True
                }
            )
        ]

        for method in ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]:
            v1_proxy.add_method(method, v1_proxy_integration,
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=auth,
                request_parameters={"method.request.path.proxy": True},
                method_responses=_method_responses
            )

        # --- Root catch-all proxy (non-/api, non-/health paths) ---
        # Catches any paths that are not /health or /api/v1/*.  In practice this
        # handles legacy or unversioned endpoints.  any_method=False for the same
        # reason as v1_proxy above: keep OPTIONS unauthenticated.
        proxy = api.root.add_proxy(
            default_integration=proxy_integration,
            any_method=False  # Don't use any_method=True as it applies auth to OPTIONS
        )

        # Add authenticated methods to proxy (all except OPTIONS)
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]:
            proxy.add_method(method, proxy_integration,
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=auth,
                request_parameters={
                    "method.request.path.proxy": True
                },
                method_responses=[
                    apigw.MethodResponse(
                        status_code="200",
                        response_parameters={
                            "method.response.header.Access-Control-Allow-Origin": True,
                            "method.response.header.Access-Control-Allow-Headers": True,
                            "method.response.header.Access-Control-Allow-Methods": True,
                            "method.response.header.Access-Control-Allow-Credentials": True
                        }
                    )
                ]
            )
        
        # Add CORS headers to API Gateway error responses
        # This is critical - without these, 401/403 errors won't have CORS headers
        # and browsers will report them as CORS errors instead of auth errors.
        # On first deploy, set to localhost. On redeployments, use the known CloudFront URL
        # (read from FRONTEND_URL env var that deploy.sh writes to backend/.env).
        acao_origin = f"'{frontend_url}'" if frontend_url and frontend_url.startswith('https://') else "'http://localhost:5173'"
        cors_headers = {
            "Access-Control-Allow-Origin": acao_origin,
            "Access-Control-Allow-Headers": "'Content-Type,Authorization,X-Amzn-Trace-Id,Cache-Control'",
            "Access-Control-Allow-Methods": "'OPTIONS,GET,PUT,POST,DELETE,PATCH,HEAD'",
            "Access-Control-Allow-Credentials": "'true'"  # Required for authentication
        }
        
        # 401 Unauthorized responses
        api.add_gateway_response("UnauthorizedResponse",
            type=apigw.ResponseType.UNAUTHORIZED,
            response_headers=cors_headers
        )
        
        # 403 Access Denied responses
        api.add_gateway_response("AccessDeniedResponse",
            type=apigw.ResponseType.ACCESS_DENIED,
            response_headers=cors_headers
        )
        
        # Default 4XX responses (catches other client errors)
        api.add_gateway_response("Default4XXResponse",
            type=apigw.ResponseType.DEFAULT_4_XX,
            response_headers=cors_headers
        )
        
        # Default 5XX responses (catches server errors)
        api.add_gateway_response("Default5XXResponse",
            type=apigw.ResponseType.DEFAULT_5_XX,
            response_headers=cors_headers
        )
        
        # Outputs
        self.api_url = api.url
        self.cognito_config = {
            "user_pool_id": user_pool.user_pool_id,
            "client_id": user_pool_client.user_pool_client_id,
            "region": self.region
        }
        
        CfnOutput(self, "APIURL", value=self.api_url)
        CfnOutput(self, "ALBEndpoint", value=alb_fargate.load_balancer.load_balancer_dns_name)
        CfnOutput(self, "CognitoUserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "CognitoWebClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "CognitoM2MClientId", value=m2m_client.user_pool_client_id)
        CfnOutput(self, "CognitoTokenEndpoint", 
            value=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com")
        CfnOutput(self, "ECRRepositoryUri", value=ecr_repo.repository_uri)
        CfnOutput(self, "ECSClusterName", value=alb_fargate.service.cluster.cluster_name)
        CfnOutput(self, "ECSServiceName", value=alb_fargate.service.service_name)
        CfnOutput(self, "ChatRequestsTableName", value=chat_table.table_name)
