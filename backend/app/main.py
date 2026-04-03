# app/main.py
"""
FastAPI main application.
Entry point for the MSP Assistant API backend.
"""

import os
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings
from app.api import routes
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create FastAPI app
_is_dev = os.getenv("ENVIRONMENT", "production") != "production"
app = FastAPI(
    title="MSP Assistant API",
    description="Backend API for AWS MSP Smart Agent Assist",
    version="2.0.0",
    docs_url="/docs" if _is_dev else None,
    redoc_url="/redoc" if _is_dev else None,
)

# CORS middleware for local development and production
allowed_origins = [
    "http://localhost:5173",  # Vite dev server
    "http://localhost:3000",  # Alternative dev
]

# Add configured frontend URL (CloudFront or custom domain)
if settings.FRONTEND_URL and settings.FRONTEND_URL != "http://localhost:5173":
    allowed_origins.append(settings.FRONTEND_URL)
    # Do NOT add http:// version — all prod traffic must use HTTPS

# Pin CORS regex to the specific CloudFront distribution if known, otherwise allow all
# CloudFront distributions as a fallback (e.g. first deploy before CLOUDFRONT_DOMAIN is set).
# Security note: the fallback allows any CloudFront distribution — replace with the
# pinned regex once CLOUDFRONT_DOMAIN is populated by deploy.sh.
if settings.CLOUDFRONT_DOMAIN:
    import re as _re
    # re.escape converts dots/hyphens in the domain to literal regex characters,
    # preventing a domain like "d1abc.cloudfront.net" from accidentally matching
    # "d1abcXcloudfrontYnet" due to unescaped regex metacharacters.
    _escaped = _re.escape(settings.CLOUDFRONT_DOMAIN)
    # Anchor to https:// only — no http:// variant; CloudFront always serves HTTPS.
    _origin_regex = rf"https://{_escaped}"
else:
    # FALLBACK: allows any CloudFront distribution — safe until CLOUDFRONT_DOMAIN is exported.
    # Pattern requires lowercase alphanumeric subdomain + literal ".cloudfront.net" over HTTPS,
    # which limits exposure to the CloudFront namespace while still supporting fresh deploys
    # where the specific distribution ID is not yet known.
    _origin_regex = r"https://[a-z0-9]+\.cloudfront\.net"

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept", "Cache-Control"],
    expose_headers=["Content-Type", "X-Request-Id"],
    max_age=600  # Cache preflight requests for 10 minutes
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append hardened security headers to every outbound HTTP response.

    Applied globally so that all routes — including error responses and
    redirects — carry these headers without requiring per-route decoration.

    Headers set and their purpose:
        X-Content-Type-Options: nosniff
            Prevents browsers from MIME-sniffing a response away from the
            declared Content-Type, closing a class of content-injection attacks.
        X-Frame-Options: DENY
            Blocks the API from being embedded in any <frame> or <iframe>,
            mitigating clickjacking.  Redundant with the CSP frame-ancestors
            directive below, but kept for older browser compatibility.
        Strict-Transport-Security: max-age=31536000; includeSubDomains
            Instructs browsers to use HTTPS for all requests to this origin
            for one year, including subdomains.  Only meaningful when the API
            is served over HTTPS (i.e., production behind CloudFront/ALB).
        Content-Security-Policy: default-src 'self'; frame-ancestors 'none'
            Restricts resource loading to the same origin and reaffirms no
            framing is permitted.
        Referrer-Policy: strict-origin-when-cross-origin
            Sends full referrer on same-origin requests; only the origin on
            cross-origin HTTPS→HTTPS; nothing on HTTPS→HTTP.
        Permissions-Policy: camera=(), microphone=(), geolocation=()
            Explicitly disables browser feature APIs that this API does not need,
            reducing the attack surface if a page ever embeds this origin.
    """
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Include API routes
app.include_router(routes.router, prefix=f"/api/{settings.API_VERSION}")

# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "service": "MSP Assistant API",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs",
        "health": "/health"
    }

# Health check endpoint (no authentication required)
@app.get("/health")
async def health_check():
    """
    Health check endpoint for monitoring.
    Returns API status and version.
    """
    return {
        "status": "healthy",
        "service": "msp-assistant-api",
        "version": "2.0.0",
        "cognito_configured": bool(settings.COGNITO_USER_POOL_ID),
        "model": settings.MODEL
    }

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Handle uncaught exceptions gracefully."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )

# Startup event
@app.on_event("startup")
async def startup_event():
    """Execute on application startup."""
    logger.info("MSP Assistant API starting up")
    logger.info(f"Region: {settings.AWS_REGION}")
    logger.info(f"Cognito User Pool: {settings.COGNITO_USER_POOL_ID}")
    logger.info(f"Model: {settings.MODEL}")

    # PRELOAD MCP clients at startup for fast first response
    try:
        logger.info("Preloading MCP clients")
        from app.core.shared_mcp_client import SharedMCPClient
        SharedMCPClient.initialize()
        logger.info("MCP clients preloaded successfully")
    except Exception as e:
        logger.warning(f"MCP preloading warning: {e}")
        # Continue startup even if MCP fails - will lazy load later

    logger.info("Application ready")

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Execute on application shutdown."""
    logger.info("MSP Assistant API shutting down")
