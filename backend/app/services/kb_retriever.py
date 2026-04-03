"""
Bedrock Knowledge Base Retriever

Minimal implementation for direct Bedrock KB access in backend.
Uses boto3 bedrock-agent-runtime retrieve() API.
"""
import os
import boto3
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# Get configuration from environment (set by ECS task definition)
BEDROCK_KNOWLEDGE_BASE_ID = os.getenv('BEDROCK_KNOWLEDGE_BASE_ID', '')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')


class BedrockKBRetriever:
    """Native AWS Bedrock Knowledge Base retriever"""

    def __init__(self, session=None):
        """
        Initialize KB retriever.
        
        Args:
            session: Optional boto3 Session (unused - KB is MSP-only resource)
        """
        # Always use default credentials (ECS task role) for KB access
        # KB exists in MSP account, not customer accounts
        self.client = boto3.client(
            'bedrock-agent-runtime',
            region_name=AWS_REGION
        )
        self.kb_id = BEDROCK_KNOWLEDGE_BASE_ID
        
        if self.kb_id:
            logger.info(f"KB Retriever initialized with KB ID: {self.kb_id[:20]}...")
        else:
            logger.warning("BEDROCK_KNOWLEDGE_BASE_ID not set - KB retrieval disabled")

    def retrieve(self, query: str, min_score: float = 0.45, max_results: int = 5) -> List[Dict]:
        """
        Retrieve remediation steps from Bedrock KB.
        
        Args:
            query: Search query
            min_score: Minimum relevance score (0.0-1.0)
            max_results: Maximum number of results
            
        Returns:
            List of dicts with 'content' and 'score' keys
        """
        if not self.kb_id:
            logger.warning("KB retrieval skipped - no KB ID configured")
            return []
        
        try:
            response = self.client.retrieve(
                knowledgeBaseId=self.kb_id,
                retrievalQuery={'text': query},
                retrievalConfiguration={
                    'vectorSearchConfiguration': {'numberOfResults': max_results}
                }
            )
            
            results = []
            for item in response.get('retrievalResults', []):
                if item['score'] >= min_score:
                    entry = {'content': item['content']['text'], 'score': item['score']}
                    # Extract source URI if available
                    location = item.get('location', {})
                    s3_loc = location.get('s3Location', {})
                    if s3_loc.get('uri'):
                        entry['source_uri'] = s3_loc['uri']
                    results.append(entry)
            
            logger.info(f"KB retrieval: {len(results)} results (min_score={min_score})")
            return results
            
        except Exception as e:
            logger.error(f"KB retrieval failed: {e}")
            return []

    def retrieve_with_fallback(
        self,
        service: str = "",
        metric_name: str = "",
        alarm_name: str = "",
        namespace: str = "",
        max_results: int = 5
    ) -> Dict:
        """
        Multi-query KB retrieval with decreasing thresholds.

        Tries 3 query strategies:
        1. Specific: "{service} {metric} {alarm} remediation troubleshooting" @ 0.45
        2. Moderate: "{service} troubleshooting remediation" @ 0.30
        3. Broad: "AWS {namespace} common issues remediation" @ 0.20

        Returns:
            Dict with results, confidence, query_used, threshold_used
        """
        # Strategy 1 (high confidence): Most specific query — combines the service name,
        # metric, and alarm name.  Threshold 0.45 ensures only highly relevant runbooks
        # are returned.  This is the ideal path when the KB contains a runbook that
        # matches the exact alarm.
        #
        # Strategy 2 (medium confidence): Drop the metric and alarm name, keep only the
        # service.  Threshold lowered to 0.30 to cast a wider net.  Used when the KB
        # has a general service troubleshooting guide but not a per-alarm runbook.
        #
        # Strategy 3 (low confidence): Broadest query using the CloudWatch namespace
        # (e.g. "AWS/ApiGateway common issues remediation").  Threshold 0.20 returns
        # anything tangentially relevant.  This is the last resort before returning empty.
        strategies = [
            {
                "query": " ".join(filter(None, [service, metric_name, alarm_name, "remediation troubleshooting"])),
                "min_score": 0.45,
                "confidence": "high",
            },
            {
                "query": " ".join(filter(None, [service, "troubleshooting remediation"])),
                "min_score": 0.30,
                "confidence": "medium",
            },
            {
                "query": " ".join(filter(None, [f"AWS {namespace}" if namespace else "AWS", "common issues remediation"])),
                "min_score": 0.20,
                "confidence": "low",
            },
        ]

        # Iterate strategies in order; return as soon as one yields results.
        # The confidence level is propagated to the caller so the frontend can
        # show a warning when remediation guidance is based on a broad/low-confidence match.
        for strategy in strategies:
            query = strategy["query"].strip()
            if not query:
                continue

            logger.info(f"KB fallback: trying '{query}' @ min_score={strategy['min_score']}")
            results = self.retrieve(query, min_score=strategy["min_score"], max_results=max_results)

            if results:
                logger.info(f"KB fallback: {len(results)} results at {strategy['confidence']} confidence")
                return {
                    "results": results,
                    "confidence": strategy["confidence"],
                    "query_used": query,
                    "threshold_used": strategy["min_score"],
                }

        logger.warning("KB fallback: no results from any strategy")
        return {
            "results": [],
            "confidence": "none",
            "query_used": "",
            "threshold_used": 0,
        }
