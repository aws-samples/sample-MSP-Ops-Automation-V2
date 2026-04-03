"""
Frontend Stack: React SPA on S3 + CloudFront CDN
=================================================
Hosts the MSP Ops Automation React frontend via CloudFront → S3, with two
additional custom behaviours layered on top of the Solutions Construct defaults.

Key design decisions:

  CloudFrontToS3 (AWS Solutions Construct)
    Provides a hardened CloudFront distribution backed by a private S3 bucket
    (OAC/OAI enforced — bucket not publicly accessible).  We opt out of the
    default security headers (insert_http_security_headers=False) so we can
    supply a custom Content-Security-Policy that allows Cognito endpoints.

  Custom Response Headers Policy (SecurityHeadersV2)
    The CSP is tailored to allow connections to Cognito IDP, Cognito Identity,
    STS, and API Gateway (both HTTPS and WSS).  Strict CSP is enforced:
    script-src and style-src are limited to 'self' only.  Cloudscape styles
    are extracted to external CSS files at build time by Vite.
    The V2 suffix and comment prevent CloudFormation from conflicting with any
    previous SecurityHeaders policy left over from earlier stack versions.

  SPA routing (CustomErrorResponses)
    S3 returns 403 (object not found behind OAC) or 404 for deep-link URLs that
    don't map to a real S3 key.  Both are rewritten to 200/index.html so React
    Router can handle client-side navigation.

  ALB SSE streaming origin (conditional)
    API Gateway (REST) buffers responses entirely and enforces a hard 29 s
    timeout — both incompatible with Server-Sent Events.  When alb_dns is
    provided, a second CloudFront origin pointing at the ALB is added, and a
    cache behaviour routes /api/v1/chat/*/stream directly there (TTL=0, no
    caching, auth headers forwarded).
    alb_dns is empty on first deploy; deploy.sh re-runs FrontendStack
    immediately after BackendStack outputs are available.

  config.json injection
    Runtime configuration (API URL, Cognito IDs, region) is written as a
    separate S3 object so the compiled React bundle does not need to be
    rebuilt when infrastructure changes — the app fetches config.json on load.

Dependencies:
  Receives api_url and cognito_config from BackendStack.
  alb_dns comes from CDK context (set by deploy.sh after BackendStack deploy).
"""
from constructs import Construct
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cloudfront,
    CfnOutput, Duration, RemovalPolicy
)
from aws_solutions_constructs.aws_cloudfront_s3 import CloudFrontToS3

class FrontendStack(Stack):
    """Frontend: React SPA on S3 with CloudFront CDN"""
    
    def __init__(self, scope: Construct, id: str, api_url: str, alb_dns: str, cognito_config: dict, **kwargs):
        super().__init__(scope, id, **kwargs)
        
        # Use AWS Solutions Construct for S3 + CloudFront
        # Note: CloudFrontToS3 creates CloudFront distribution with secure defaults automatically
        # We will override the response headers policy after creation
        cloudfront_s3 = CloudFrontToS3(self, "Frontend",
            bucket_props=s3.BucketProps(
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
                versioned=False
            ),
            insert_http_security_headers=False  # Disable default security headers
        )
        
        # Create custom Response Headers Policy with proper CSP for Cognito
        # Note: Using V2 suffix and unique comment to avoid CloudFormation state conflicts
        response_headers_policy = cloudfront.ResponseHeadersPolicy(self, "SecurityHeadersV2",
            comment="MSP Assistant CSP Policy v2 - CloudFront CORS Fix",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self' "
                        f"https://cognito-idp.{self.region}.amazonaws.com "
                        f"https://cognito-identity.{self.region}.amazonaws.com "
                        f"https://sts.{self.region}.amazonaws.com; "
                        "script-src 'self'; "
                        "style-src 'self'; "
                        "connect-src 'self' "
                        f"https://cognito-idp.{self.region}.amazonaws.com "
                        f"https://cognito-identity.{self.region}.amazonaws.com "
                        f"https://sts.{self.region}.amazonaws.com "
                        f"https://*.execute-api.{self.region}.amazonaws.com "
                        f"wss://*.execute-api.{self.region}.amazonaws.com; "
                        "font-src 'self' data:; "
                        "img-src 'self' data: https:;"
                    ),
                    override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.seconds(31536000),
                    include_subdomains=True,
                    override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True,
                    mode_block=True,
                    override=True
                )
            )
        )
        
        # CDK L2 CloudFrontWebDistribution does not expose ResponseHeadersPolicyId as
        # a typed property, so we use the CloudFormation escape-hatch (node.default_child
        # returns the underlying CfnDistribution) to inject it via add_property_override.
        cfn_distribution = cloudfront_s3.cloud_front_web_distribution.node.default_child
        cfn_distribution.add_property_override(
            'DistributionConfig.DefaultCacheBehavior.ResponseHeadersPolicyId',
            response_headers_policy.response_headers_policy_id
        )
        
        # Add custom error responses for SPA routing
        # When S3 returns 403/404 for non-existent paths, serve index.html instead
        # This allows React Router to handle client-side routing
        cfn_distribution.add_property_override(
            "DistributionConfig.CustomErrorResponses",
            [
                {
                    "ErrorCode": 403,
                    "ResponseCode": 200,
                    "ResponsePagePath": "/index.html",
                    "ErrorCachingMinTTL": 10
                },
                {
                    "ErrorCode": 404,
                    "ResponseCode": 200,
                    "ResponsePagePath": "/index.html",
                    "ErrorCachingMinTTL": 10
                }
            ]
        )
        
        # Add ALB as second origin for SSE streaming (bypasses API Gateway buffering)
        # REST API Gateway buffers responses and has a 29s timeout — SSE events never
        # reach the client. Routing the stream path through CloudFront → ALB lets
        # chunked transfer-encoded SSE events flow in real-time.
        # alb_dns is empty on first CDK deploy (outputs.json not yet created).
        # deploy.sh re-deploys FrontendStack immediately after with the correct ALB DNS.
        if alb_dns:
            cfn_distribution.add_property_override(
                "DistributionConfig.Origins.1",
                {
                    "DomainName": alb_dns,
                    "Id": "ALBStreamOrigin",
                    "CustomOriginConfig": {
                        "HTTPPort": 80,
                        "OriginProtocolPolicy": "http-only",
                        # 60 s read timeout keeps SSE connections open long enough for
                        # multi-step workflow responses; CloudFront's maximum is 60 s.
                        # Keepalive matches read timeout to avoid premature connection closure.
                        "OriginReadTimeout": 60,
                        "OriginKeepaliveTimeout": 60,
                    }
                }
            )

            # Cache behavior: route SSE stream requests to ALB (no caching, forward auth headers)
            cfn_distribution.add_property_override(
                "DistributionConfig.CacheBehaviors",
                [
                    {
                        "PathPattern": "api/v1/chat/*/stream",
                        "TargetOriginId": "ALBStreamOrigin",
                        "ViewerProtocolPolicy": "https-only",
                        "AllowedMethods": ["GET", "HEAD", "OPTIONS"],
                        "CachedMethods": ["GET", "HEAD"],
                        "ForwardedValues": {
                            "QueryString": True,
                            "Headers": ["Authorization", "Accept", "Cache-Control"],
                            "Cookies": {"Forward": "all"}
                        },
                        "DefaultTTL": 0,
                        "MinTTL": 0,
                        "MaxTTL": 0,
                    }
                ]
            )

        # Deploy frontend build artifacts
        s3deploy.BucketDeployment(self, "DeployFrontend",
            sources=[s3deploy.Source.asset("../../frontend/dist")],
            destination_bucket=cloudfront_s3.s3_bucket,
            distribution=cloudfront_s3.cloud_front_web_distribution,
            distribution_paths=["/*"]
        )
        
        # Inject runtime config for frontend
        runtime_config = {
            "apiUrl": api_url,
            "cognitoUserPoolId": cognito_config["user_pool_id"],
            "cognitoClientId": cognito_config["client_id"],
            "region": cognito_config["region"]
        }
        
        s3deploy.BucketDeployment(self, "DeployConfig",
            sources=[s3deploy.Source.json_data("config.json", runtime_config)],
            destination_bucket=cloudfront_s3.s3_bucket
        )
        
        # Outputs
        CfnOutput(self, "FrontendURL",
            value=f"https://{cloudfront_s3.cloud_front_web_distribution.distribution_domain_name}",
            description="CloudFront URL for frontend"
        )
        CfnOutput(self, "S3BucketName",
            value=cloudfront_s3.s3_bucket.bucket_name
        )