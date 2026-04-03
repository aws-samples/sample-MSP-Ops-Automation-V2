"""
Robust Date Parsing Module
Industry-proven hybrid approach: LLM + specialized libraries for natural language date parsing
Based on research from MCP tools on best practices for business applications
"""

import json
import logging
import re
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from dateutil import parser as dateutil_parser
import parsedatetime
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DateRange:
    """Structured date range with metadata"""
    start_date: str  # ISO 8601 format YYYY-MM-DD
    end_date: str    # ISO 8601 format YYYY-MM-DD
    period_days: int
    confidence: float  # 0.0 to 1.0
    interpretation: str
    method_used: str


class RobustDateParser:
    """
    Industry-proven hybrid date parser combining:
    1. LLM with structured output (primary)
    2. Specialized parsing libraries (fallback)
    3. Business context awareness
    4. ISO 8601 standardization
    """
    
    def __init__(self, llm_agent=None, timezone='America/Chicago'):
        """
        Initialize parser with business context
        
        Args:
            llm_agent: Strands Agent for LLM parsing
            timezone: Default timezone for business context
        """
        self.llm_agent = llm_agent
        self.timezone = timezone
        self.current_date = datetime.now()
        
        # Initialize parsedatetime library for natural language parsing
        self.cal = parsedatetime.Calendar()
        
        # Business configuration
        self.fiscal_year_start_month = 1  # January (can be configured per business)
        self.weekend_start = 6  # Saturday (0=Monday, 6=Saturday)
        
    def parse_time_period(self, query: str) -> DateRange:
        """
        Main parsing method using hybrid approach
        
        Args:
            query: Natural language query containing time period
            
        Returns:
            DateRange with structured date information
        """
        try:
            # Method 1: Enhanced LLM with structured JSON output
            llm_result = self._parse_with_llm_structured(query)
            if llm_result and llm_result.confidence >= 0.8:
                return llm_result
                
            # Method 2: Specialized library parsing
            lib_result = self._parse_with_libraries(query)
            if lib_result and lib_result.confidence >= 0.7:
                return lib_result
                
            # Method 3: Enhanced regex patterns
            regex_result = self._parse_with_enhanced_regex(query)
            if regex_result and regex_result.confidence >= 0.6:
                return regex_result
                
            # Method 4: Fallback to conservative default
            return self._fallback_parse(query)
            
        except Exception as e:
            logger.error(f"Date parsing error: {e}")
            return self._fallback_parse(query)
    
    def _parse_with_llm_structured(self, query: str) -> Optional[DateRange]:
        """
        Use LLM with structured JSON output for precise parsing
        """
        if not self.llm_agent:
            return None
            
        try:
            current_date_str = self.current_date.strftime('%Y-%m-%d')
            current_day_of_month = self.current_date.day
            days_since_jan1 = (self.current_date - datetime(self.current_date.year, 1, 1)).days + 1
            
            analysis_prompt = f"""
            CURRENT CONTEXT:
            - Today's date: {current_date_str}
            - Current day of month: {current_day_of_month}
            - Days since January 1st: {days_since_jan1}
            - Current fiscal year starts: January (month 1)
            
            USER QUERY: "{query}"
            
            Parse this query and return ONLY a JSON object with exact calculations:
            
            {{
                "start_date": "YYYY-MM-DD",
                "end_date": "YYYY-MM-DD",
                "period_days": integer,
                "confidence": 0.0-1.0,
                "interpretation": "detailed explanation"
            }}
            
            CALCULATION RULES:
            - "last X months" = X * 30 days backward from today
            - "last X years" = X * 365 days backward from today  
            - "current month" = from start of {self.current_date.strftime('%B')} to today ({current_day_of_month} days)
            - "year to date" = from January 1st to today ({days_since_jan1} days)
            - "quarter" = 90 days (3 months)
            - "last week" = 7 days
            
            EXAMPLES:
            - "last 4 months" → period_days: 120, start_date: 4 months ago
            - "last 6 months" → period_days: 180, start_date: 6 months ago
            - "current bill" → period_days: {current_day_of_month}, start_date: start of current month
            
            Return ONLY the JSON object, no other text.
            """
            
            response = self.llm_agent(analysis_prompt)
            
            # Handle AgentResult object - extract the text content
            response_text = str(response)
            if hasattr(response, 'content'):
                response_text = response.content
            elif hasattr(response, 'text'):
                response_text = response.text
            
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                
                # Validate required fields
                required_fields = ['start_date', 'end_date', 'period_days', 'confidence', 'interpretation']
                if all(field in data for field in required_fields):
                    # Validate date formats
                    start_date = datetime.strptime(data['start_date'], '%Y-%m-%d').date()
                    end_date = datetime.strptime(data['end_date'], '%Y-%m-%d').date()
                    
                    return DateRange(
                        start_date=data['start_date'],
                        end_date=data['end_date'],
                        period_days=data['period_days'],
                        confidence=float(data['confidence']),
                        interpretation=data['interpretation'],
                        method_used='LLM_STRUCTURED'
                    )
                    
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"LLM structured parsing failed: {e}")
            return None
    
    def _parse_with_libraries(self, query: str) -> Optional[DateRange]:
        """
        Use specialized libraries for natural language parsing
        """
        try:
            query_lower = query.lower()
            
            # Method 2A: parsedatetime library for complex expressions
            time_struct, parse_status = self.cal.parse(query_lower)
            if parse_status > 0:
                parsed_date = datetime(*time_struct[:6])
                
                # Determine if it's a duration or specific date
                if any(word in query_lower for word in ['last', 'past', 'previous', 'ago']):
                    # It's a duration - calculate period from parsed reference
                    end_date = self.current_date.date()
                    period_days = (end_date - parsed_date.date()).days
                    
                    if 0 < period_days <= 1095:  # Reasonable bounds
                        return DateRange(
                            start_date=parsed_date.strftime('%Y-%m-%d'),
                            end_date=end_date.strftime('%Y-%m-%d'),
                            period_days=period_days,
                            confidence=0.75,
                            interpretation=f"Parsed '{query}' as {period_days} days using parsedatetime",
                            method_used='PARSEDATETIME'
                        )
            
            # Method 2B: dateutil with relativedelta for month/year calculations
            number_match = re.search(r'(\d+)\s*(month|year|day|week)', query_lower)
            if number_match:
                number = int(number_match.group(1))
                unit = number_match.group(2)
                
                end_date = self.current_date.date()
                
                if 'month' in unit:
                    start_date = (self.current_date - relativedelta(months=number)).date()
                    period_days = number * 30  # Approximation
                elif 'year' in unit:
                    start_date = (self.current_date - relativedelta(years=number)).date()
                    period_days = number * 365  # Approximation
                elif 'week' in unit:
                    start_date = (self.current_date - timedelta(weeks=number)).date()
                    period_days = number * 7
                elif 'day' in unit:
                    start_date = (self.current_date - timedelta(days=number)).date()
                    period_days = number
                else:
                    return None
                
                return DateRange(
                    start_date=start_date.strftime('%Y-%m-%d'),
                    end_date=end_date.strftime('%Y-%m-%d'),
                    period_days=period_days,
                    confidence=0.8,
                    interpretation=f"Calculated {number} {unit}(s) using dateutil",
                    method_used='DATEUTIL'
                )
                
        except Exception as e:
            logger.warning(f"Library parsing failed: {e}")
            return None
    
    def _parse_with_enhanced_regex(self, query: str) -> Optional[DateRange]:
        """
        Enhanced regex patterns for common business expressions
        """
        try:
            query_lower = query.lower()
            end_date = self.current_date.date()
            
            # Business-specific patterns
            patterns = {
                # Months
                r'last (\d+) months?': lambda m: int(m.group(1)) * 30,
                r'past (\d+) months?': lambda m: int(m.group(1)) * 30,
                r'(\d+) months? ago': lambda m: int(m.group(1)) * 30,
                
                # Years  
                r'last (\d+) years?': lambda m: int(m.group(1)) * 365,
                r'past (\d+) years?': lambda m: int(m.group(1)) * 365,
                
                # Quarters
                r'last quarter': lambda m: 90,
                r'this quarter': lambda m: self._get_quarter_days(),
                r'q[1-4]': lambda m: 90,
                
                # Weeks
                r'last (\d+) weeks?': lambda m: int(m.group(1)) * 7,
                r'past (\d+) weeks?': lambda m: int(m.group(1)) * 7,
                
                # Current period variations
                r'current month': lambda m: self.current_date.day,
                r'this month': lambda m: self.current_date.day,
                r'current bill': lambda m: self.current_date.day,
                r'month to date': lambda m: self.current_date.day,
                r'mtd': lambda m: self.current_date.day,
                
                # Year to date
                r'year to date': lambda m: (self.current_date - datetime(self.current_date.year, 1, 1)).days + 1,
                r'ytd': lambda m: (self.current_date - datetime(self.current_date.year, 1, 1)).days + 1,
            }
            
            for pattern, calculator in patterns.items():
                match = re.search(pattern, query_lower)
                if match:
                    period_days = calculator(match)
                    
                    if 0 < period_days <= 1095:  # Reasonable bounds
                        start_date = (self.current_date - timedelta(days=period_days)).date()
                        
                        return DateRange(
                            start_date=start_date.strftime('%Y-%m-%d'),
                            end_date=end_date.strftime('%Y-%m-%d'),
                            period_days=period_days,
                            confidence=0.7,
                            interpretation=f"Regex pattern matched: '{pattern}' → {period_days} days",
                            method_used='ENHANCED_REGEX'
                        )
                        
        except Exception as e:
            logger.warning(f"Regex parsing failed: {e}")
            return None
    
    def _get_quarter_days(self) -> int:
        """Calculate days elapsed in current quarter"""
        current_month = self.current_date.month
        
        # Determine quarter start month
        if current_month <= 3:
            quarter_start = 1  # January
        elif current_month <= 6:
            quarter_start = 4  # April  
        elif current_month <= 9:
            quarter_start = 7  # July
        else:
            quarter_start = 10  # October
            
        quarter_start_date = datetime(self.current_date.year, quarter_start, 1)
        return (self.current_date - quarter_start_date).days + 1
    
    def _fallback_parse(self, query: str) -> DateRange:
        """
        Conservative fallback when other methods fail
        """
        # Default to 30 days (1 month) as safe fallback
        period_days = 30
        end_date = self.current_date.date()
        start_date = (self.current_date - timedelta(days=period_days)).date()
        
        return DateRange(
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
            period_days=period_days,
            confidence=0.3,
            interpretation=f"Fallback: defaulted to {period_days} days due to parsing uncertainty",
            method_used='FALLBACK'
        )
    
    def get_test_cases(self) -> Dict[str, DateRange]:
        """
        Generate test cases for validation
        """
        test_queries = [
            "last 4 months",
            "last 6 months", 
            "last year",
            "year to date",
            "current month",
            "current bill",
            "last quarter",
            "last 3 weeks",
            "past 2 years",
            "this month"
        ]
        
        results = {}
        for query in test_queries:
            results[query] = self.parse_time_period(query)
            
        return results


def create_robust_date_parser(llm_agent=None) -> RobustDateParser:
    """
    Factory function to create configured date parser
    
    Args:
        llm_agent: Strands Agent for LLM parsing
        
    Returns:
        Configured RobustDateParser instance
    """
    return RobustDateParser(llm_agent=llm_agent)


# Test validation function
def validate_date_parser():
    """
    Validation function for testing the robust date parser
    """
    parser = RobustDateParser()
    test_cases = parser.get_test_cases()
    
    logger.info("Date Parser Validation Results:")
    logger.info("=" * 60)

    for query, result in test_cases.items():
        logger.info(f"Query: '{query}'")
        logger.info(f"  Period: {result.period_days} days ({result.start_date} to {result.end_date})")
        logger.info(f"  Confidence: {result.confidence:.1%}")
        logger.info(f"  Method: {result.method_used}")
        logger.info(f"  Interpretation: {result.interpretation}")
    
    return test_cases


if __name__ == "__main__":
    # Run validation when script is executed directly
    validate_date_parser()
