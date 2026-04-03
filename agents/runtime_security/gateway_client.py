"""Gateway MCP client with SigV4 authentication and cold-start retry.

The AgentCore Gateway uses AWS_IAM (SigV4) for inbound auth.
MCP servers on AgentCore Runtime may be idle and need cold-start (10-30s).
This client handles that with retry logic and reduced timeouts.

Cold-start strategy: Lazy reconnect on failure (no keepalive thread).
MCP servers configured with --idle-timeout 1800s (30 min) in deploy.sh,
so sessions stay warm between user queries without pinging. A background
keepalive thread causes more problems than it solves (race conditions with
in-flight tool calls, constant disconnect/reconnect cycles).
"""
import os
import time
import logging
import httpx
from typing import Generator, AsyncGenerator
from botocore.session import Session as BotocoreSession
from botocore.auth import SigV4Auth as BotocoreSigV4Auth
from botocore.awsrequest import AWSRequest
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

# Cold-start config: MCP servers may need up to 30s to spin up a microVM
# Timeout reduced to 300s — keepalive is not used, lazy reconnect handles failures
MCP_TIMEOUT_SECONDS = 300   # 5 minutes (sufficient for slowest tools: Security Hub ~130s)
MCP_RETRY_ATTEMPTS = 3      # retry on transient cold-start errors
MCP_RETRY_DELAY = 1         # 1 second between retries (reduced from 5s to keep init <30s)


def _load_env_file():
    for name in ['env_config.txt', '.env']:
        for base in [os.path.dirname(__file__), os.getcwd()]:
            env_path = os.path.join(base, name)
            if os.path.exists(env_path):
                with open(env_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            os.environ.setdefault(key.strip(), val.strip())
                return

_load_env_file()


class SigV4Auth(httpx.Auth):
    """HTTPX Auth that signs every request with AWS SigV4."""

    def __init__(self, service='bedrock-agentcore', region=None):
        self.service = service
        self.region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._session = BotocoreSession()

    def _sign(self, request: httpx.Request) -> httpx.Request:
        creds = self._session.get_credentials().get_frozen_credentials()
        body = request.content.decode() if request.content else ''
        headers = {'host': request.headers['host']}
        if 'content-type' in request.headers:
            headers['content-type'] = request.headers['content-type']
        aws_req = AWSRequest(method=request.method, url=str(request.url), headers=headers, data=body)
        BotocoreSigV4Auth(creds, self.service, self.region).add_auth(aws_req)
        for k in ('Authorization', 'X-Amz-Date', 'X-Amz-Security-Token'):
            if k in aws_req.headers:
                request.headers[k] = aws_req.headers[k]
        return request

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield self._sign(request)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        yield self._sign(request)


def create_gateway_mcp_client():
    """Create MCPClient connected to Gateway with SigV4 auth and cold-start resilient timeouts."""
    gateway_url = os.getenv('GATEWAY_URL')
    if not gateway_url:
        raise ValueError("GATEWAY_URL not set")

    region = os.environ.get('AWS_REGION', 'us-east-1')
    logger.info(f"Creating Gateway MCP client: {gateway_url} (SigV4, {region}, timeout={MCP_TIMEOUT_SECONDS}s)")

    return MCPClient(
        lambda: streamablehttp_client(
            gateway_url,
            auth=SigV4Auth(region=region),
            timeout=MCP_TIMEOUT_SECONDS,
        )
    )


class ResilientMCPClientManager:
    """Manages MCP client lifecycle with lazy reconnect on failure.

    When the Gateway returns an error because the MCP server is cold-starting,
    this manager retries the connection rather than letting the LLM see a raw
    error and hallucinate an "authentication issue" response.

    Design: No background keepalive thread — MCP servers are configured with
    30-min idle timeout (deploy.sh --idle-timeout 1800), so sessions stay warm
    between user queries without pinging. Reconnect happens lazily when
    list_tools_sync() or a tool call fails.
    """

    def __init__(self):
        self._client = None

    def get_client(self) -> MCPClient:
        if self._client is None:
            self._client = create_gateway_mcp_client()
            self._connect_with_retry()
        return self._client

    def get_tools_with_retry(self):
        """Get MCP tools, reconnecting if the session has expired."""
        client = self.get_client()
        try:
            return client.list_tools_sync()
        except Exception as e:
            logger.warning(f"list_tools_sync failed ({e}), reconnecting...")
            self.reconnect()
            return self._client.list_tools_sync()

    def _connect_with_retry(self):
        """Connect to Gateway, retrying if MCP server is cold-starting."""
        for attempt in range(1, MCP_RETRY_ATTEMPTS + 1):
            try:
                self._client.__enter__()
                logger.info("Gateway MCP client connected")
                return
            except Exception as e:
                logger.warning(f"MCP connect attempt {attempt}/{MCP_RETRY_ATTEMPTS} failed: {e}")
                if attempt < MCP_RETRY_ATTEMPTS:
                    time.sleep(MCP_RETRY_DELAY)  # nosemgrep: arbitrary-sleep -- intentional: retry backoff for MCP server cold-start
                else:
                    logger.warning("All MCP connect attempts failed, raising")
                    raise

    def reconnect(self):
        """Force reconnect — call this when a tool call fails with a cold-start error."""
        logger.info("Reconnecting Gateway MCP client...")
        try:
            if self._client:
                self._client.__exit__(None, None, None)
        except Exception:  # nosec B110
            pass
        self._client = create_gateway_mcp_client()
        self._connect_with_retry()

    def close(self):
        if self._client:
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass
            self._client = None