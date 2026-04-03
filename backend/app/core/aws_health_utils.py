"""
AWS Health Dashboard Utilities

This module provides functions to fetch AWS Health events using boto3.
Displays global outages, scheduled changes, and account notifications in the Streamlit UI.
"""

import boto3
import logging
from datetime import datetime
import json
import os

from app.core.config import settings

logger = logging.getLogger(__name__)


def generate_event_summary(events, category):
    """
    Generate a one-sentence LLM summary of health events.
    
    Args:
        events: List of health events
        category: Event category (issue, scheduledChange, accountNotification)
    
    Returns:
        str: One-sentence summary
    """
    if not events:
        return "No events detected in this category."
    
    try:
        # Create Bedrock Runtime client
        bedrock = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_REGION', 'us-east-1'))
        
        # Build event context
        event_details = []
        for event in events[:5]:  # Limit to first 5 events
            service = event.get('service', 'AWS')
            region = event.get('region', 'global')
            status = event.get('statusCode', 'open')
            event_details.append(f"{service} ({region}) - {status}")
        
        context = ", ".join(event_details)
        
        # Create prompt for summary
        prompt = f"""Summarize these AWS Health {category} events in ONE sentence (max 15 words):
{context}

Summary:"""
        
        # Call Bedrock Claude
        response = bedrock.invoke_model(
            modelId=settings.MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 50,
                "messages": [{
                    "role": "user",
                    "content": prompt
                }],
                "temperature": 0.3
            })
        )
        
        # Parse response
        result = json.loads(response['body'].read())
        summary = result['content'][0]['text'].strip()
        
        return summary
        
    except Exception as e:
        logger.warning(f"LLM summary generation failed: {e}")
        # Fallback to simple summary
        return f"{len(events)} {category} event{'s' if len(events) != 1 else ''} detected."


def get_event_details(event_arns):
    """
    Fetch detailed information for specific AWS Health events.
    
    Args:
        event_arns: List of event ARNs to fetch details for
    
    Returns:
        dict: Detailed event information including descriptions and metadata
    """
    try:
        client = boto3.client('health', region_name='us-east-1')
        
        # Fetch event details
        response = client.describe_event_details(eventArns=event_arns)
        
        # Parse successful and failed results
        successful = response.get('successfulSet', [])
        failed = response.get('failedSet', [])
        
        # Log results
        logger.info(f"Retrieved details for {len(successful)} events, {len(failed)} failed")
        
        return {
            "success": True,
            "details": successful,
            "failed": failed
        }
        
    except Exception as e:
        logger.error(f"Error fetching event details: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }


def get_health_events(event_type_category=None, event_status=None, service=None, region=None, include_details=True):
    """
    Fetch AWS Health events using boto3 with optional detailed descriptions.
    
    Args:
        event_type_category: Filter by category (issue, accountNotification, scheduledChange)
        event_status: Filter by status (open, closed, upcoming)
        service: Filter by AWS service (e.g., EC2, RDS)
        region: Filter by AWS region (e.g., us-east-1)
        include_details: If True, fetch full event descriptions and metadata
    
    Returns:
        dict: Health events data or error message
    """
    try:
        # Create boto3 Health client
        # AWS Health API requires us-east-1 endpoint (global service)
        client = boto3.client('health', region_name='us-east-1')
        
        # Build filter dictionary
        filters = {}
        if event_type_category:
            filters['eventTypeCategories'] = [event_type_category]
        if event_status:
            filters['eventStatusCodes'] = [event_status.lower()]  # AWS requires lowercase: open, closed, upcoming
        if service:
            filters['services'] = [service]
        if region:
            filters['regions'] = [region]
        
        # Build request parameters
        params = {'maxResults': 20}
        if filters:
            params['filter'] = filters
        
        # Log request for debugging
        logger.info(f"AWS Health API Request: describe_events with filters {filters}")
        
        # Call AWS Health API
        response = client.describe_events(**params)
        events = response.get('events', [])
        
        # Log response for debugging
        logger.info(f"AWS Health API Success: Retrieved {len(events)} events")
        
        # Fetch detailed descriptions if requested
        if include_details and events:
            event_arns = [event['arn'] for event in events]
            details_response = get_event_details(event_arns)
            
            if details_response.get('success'):
                # Create a mapping of ARN to details
                details_map = {
                    detail['event']['arn']: detail 
                    for detail in details_response.get('details', [])
                }
                
                # Enrich events with detailed descriptions
                for event in events:
                    arn = event['arn']
                    if arn in details_map:
                        detail = details_map[arn]
                        event['eventDescription'] = detail.get('eventDescription', {})
                        event['eventMetadata'] = detail.get('eventMetadata', {})
        
        return {
            "success": True,
            "events": events,
            "count": len(events),
            "fetched_at": datetime.now().isoformat()
        }
            
    except client.exceptions.UnsupportedLocale as e:
        logger.error("AWS Health API Error: Unsupported locale")
        return {
            "success": False,
            "error": "Unsupported locale for AWS Health API",
            "note": "AWS Health API requires Business Support+ or Enterprise Support plan."
        }
    except Exception as e:
        logger.error(f"AWS Health API Exception Type: {type(e).__name__}")
        logger.error(f"AWS Health API Exception: {str(e)}")
        import traceback
        logger.error(f"AWS Health API Traceback:\n{traceback.format_exc()}")
        
        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
            "note": "AWS Health API requires Business Support+ or Enterprise Support plan."
        }


def get_global_outages():
    """
    Get active AWS service issues (outages).
    
    Returns:
        dict: Active issue events
    """
    return get_health_events(event_type_category="issue", event_status="open")


def get_scheduled_changes():
    """
    Get scheduled AWS maintenance events.
    
    Returns:
        dict: Scheduled change events
    """
    return get_health_events(event_type_category="scheduledChange")


def get_account_notifications():
    """
    Get AWS account notifications.
    
    Returns:
        dict: Account notification events
    """
    return get_health_events(event_type_category="accountNotification")


def get_event_summary():
    """
    Get summary of AWS Health events by category.
    
    Returns:
        dict: Event count summary
    """
    try:
        # Create boto3 Health client
        # AWS Health API requires us-east-1 endpoint (global service)
        client = boto3.client('health', region_name='us-east-1')
        
        # Log request for debugging
        logger.info("AWS Health API Request: describe_event_aggregates")
        
        # Call AWS Health API for aggregates
        response = client.describe_event_aggregates(aggregateField='eventTypeCategory')
        
        aggregates = response.get('eventAggregates', [])
        
        summary = {
            "success": True,
            "total": 0,
            "by_category": {}
        }
        
        for agg in aggregates:
            category = agg.get('aggregateValue', 'Unknown')
            count = agg.get('count', 0)
            summary["by_category"][category] = count
            summary["total"] += count
        
        summary["fetched_at"] = datetime.now().isoformat()
        
        # Log response for debugging
        logger.info(f"AWS Health API Success: Retrieved {summary['total']} total events across {len(summary['by_category'])} categories")
        
        return summary
            
    except Exception as e:
        logger.error(f"AWS Health Summary Exception Type: {type(e).__name__}")
        logger.error(f"AWS Health Summary Exception: {str(e)}")
        import traceback
        logger.error(f"AWS Health Summary Traceback:\n{traceback.format_exc()}")
        
        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
            "note": "AWS Health API requires Business Support+ or Enterprise Support plan."
        }


def format_event_for_display(event):
    """
    Format a health event for UI display.
    
    Args:
        event: Health event dict
    
    Returns:
        dict: Formatted event data
    """
    return {
        "service": event.get("service", "AWS"),
        "event_type": event.get("eventTypeCode", "Unknown"),
        "category": event.get("eventTypeCategory", "unknown"),
        "region": event.get("region", "global"),
        "start_time": event.get("startTime", ""),
        "end_time": event.get("endTime", ""),
        "status": event.get("statusCode", "open"),
        "description": event.get("eventDescription", {}).get("latestDescription", "No description available")[:200]
    }


def get_health_status_icon(category, status="open"):
    """
    Get status label for health status.

    Args:
        category: Event category
        status: Event status

    Returns:
        str: Status label
    """
    if category == "issue" and status == "open":
        return "CRITICAL"
    elif category == "scheduledChange":
        return "HIGH"
    elif category == "accountNotification":
        return "INFO"
    else:
        return "OK"
