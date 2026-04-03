# backend/app/services/health_service.py
"""
Health Dashboard service wrapping existing AWS Health utilities.
Provides REST API interface for AWS Health events and status monitoring.
"""

import sys
import os
from typing import Dict, List
from datetime import datetime

# Add parent directory to path to import existing modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.core import aws_health_utils


class HealthService:
    """
    Service for AWS Health Dashboard data.
    Wraps existing aws_health_utils.py logic for REST API access.
    """
    
    async def get_health_summary(self) -> Dict:
        """
        Get summary of AWS Health events by category.
        
        Returns:
            Dict with event counts and summary data
        """
        try:
            summary = aws_health_utils.get_event_summary()
            
            return {
                "success": summary.get("success", False),
                "data": {
                    "total": summary.get("total", 0),
                    "by_category": summary.get("by_category", {}),
                    "fetched_at": summary.get("fetched_at")
                },
                "error": summary.get("error"),
                "note": summary.get("note")
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Health summary failed: {str(e)}",
                "note": "AWS Health API requires Business Support+ plan"
            }
    
    async def get_outages(self) -> Dict:
        """
        Get active AWS service issues (global outages).
        
        Returns:
            Dict with active issue events
        """
        try:
            result = aws_health_utils.get_global_outages()
            
            if result.get("success"):
                events = result.get("events", [])
                
                # Format events for frontend
                formatted_events = [
                    self._format_event(event) for event in events
                ]
                
                return {
                    "success": True,
                    "events": formatted_events,
                    "count": len(formatted_events),
                    "fetched_at": result.get("fetched_at")
                }
            else:
                return {
                    "success": False,
                    "error": result.get("error"),
                    "note": result.get("note"),
                    "events": []
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch outages: {str(e)}",
                "events": []
            }
    
    async def get_scheduled_maintenance(self) -> Dict:
        """
        Get scheduled AWS maintenance events.
        
        Returns:
            Dict with scheduled change events
        """
        try:
            result = aws_health_utils.get_scheduled_changes()
            
            if result.get("success"):
                events = result.get("events", [])
                
                # Format events for frontend
                formatted_events = [
                    self._format_event(event) for event in events
                ]
                
                return {
                    "success": True,
                    "events": formatted_events,
                    "count": len(formatted_events),
                    "fetched_at": result.get("fetched_at")
                }
            else:
                return {
                    "success": False,
                    "error": result.get("error"),
                    "note": result.get("note"),
                    "events": []
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch scheduled maintenance: {str(e)}",
                "events": []
            }
    
    async def get_notifications(self) -> Dict:
        """
        Get AWS account notifications.
        
        Returns:
            Dict with account notification events
        """
        try:
            result = aws_health_utils.get_account_notifications()
            
            if result.get("success"):
                events = result.get("events", [])
                
                # Format events for frontend
                formatted_events = [
                    self._format_event(event) for event in events
                ]
                
                return {
                    "success": True,
                    "events": formatted_events,
                    "count": len(formatted_events),
                    "fetched_at": result.get("fetched_at")
                }
            else:
                return {
                    "success": False,
                    "error": result.get("error"),
                    "note": result.get("note"),
                    "events": []
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to fetch notifications: {str(e)}",
                "events": []
            }
    
    def _format_event(self, event: Dict) -> Dict:
        """
        Format health event for frontend consumption.
        
        Args:
            event: Raw health event from AWS API
            
        Returns:
            Formatted event dict
        """
        return {
            "service": event.get("service", "AWS"),
            "region": event.get("region", "global"),
            "event_type": event.get("eventTypeCode", "Unknown"),
            "category": event.get("eventTypeCategory", "unknown"),
            "status": event.get("statusCode", "open"),
            "start_time": event.get("startTime"),
            "end_time": event.get("endTime"),
            "description": event.get("eventDescription", {}).get("latestDescription", "No description available")[:500],
            "metadata": event.get("eventMetadata", {})
        }


# Singleton instance
_health_service = None

def get_health_service() -> HealthService:
    """Get singleton HealthService instance."""
    global _health_service
    if _health_service is None:
        _health_service = HealthService()
    return _health_service
