"""
Supabase database client and query functions.
Handles all database operations with proper error handling and security.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client

from config import get_settings

logger = logging.getLogger(__name__)

# Allowed tables for querying
ALLOWED_TABLES = {
    "users",
    "outlets",
    "order_items_new",
    "items",
    "ledger",
    "outlet_menus",
    "inventory_items"
}

# Forbidden SQL keywords (for safety)
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "EXECUTE", "EXEC", "MERGE",
    "CALL", "COPY", "VACUUM", "REINDEX", "CLUSTER"
}


class DatabaseClient:
    """Supabase database client wrapper with query execution capabilities."""
    
    _instance: Optional["DatabaseClient"] = None
    _client: Optional[Client] = None
    
    def __new__(cls) -> "DatabaseClient":
        """Singleton pattern to reuse the database client."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        """Initialize the Supabase client."""
        if self._client is None:
            settings = get_settings()
            try:
                print("Supabase URL: ", settings.supabase_url)
                print("Supabase Key: ", settings.supabase_key)
                self._client = create_client(
                    settings.supabase_url,
                    settings.supabase_key
                )
                logger.info("Supabase client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Supabase client: {e}")
                raise
    
    @property
    def client(self) -> Client:
        """Get the Supabase client instance."""
        if self._client is None:
            raise RuntimeError("Database client not initialized")
        return self._client
    
    async def test_connection(self) -> bool:
        """Test the database connection."""
        try:
            # Try to fetch a single row from outlets table
            result = self.client.table("outlets").select("outlet_id").limit(1).execute()
            return True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False
    
    async def get_outlet_by_name(self, outlet_name: str) -> Optional[Dict[str, Any]]:
        """
        Get outlet information by outlet name.
        
        Args:
            outlet_name: The name of the outlet to find
            
        Returns:
            Outlet data dictionary or None if not found
        """
        try:
            result = self.client.table("outlets") \
                .select("*") \
                .ilike("outlet_name", outlet_name) \
                .limit(1) \
                .execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0]
            
            # Try partial match if exact match fails
            result = self.client.table("outlets") \
                .select("*") \
                .ilike("outlet_name", f"%{outlet_name}%") \
                .limit(1) \
                .execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0]
            
            return None
            
        except Exception as e:
            logger.error(f"Error fetching outlet by name '{outlet_name}': {e}")
            return None
    
    async def get_outlet_id(self, outlet_name: str) -> Optional[str]:
        """
        Get the outlet_id for a given outlet name.
        
        Args:
            outlet_name: The name of the outlet
            
        Returns:
            outlet_id string or None if not found
        """
        outlet = await self.get_outlet_by_name(outlet_name)
        return outlet.get("outlet_id") if outlet else None
    
    def validate_query(self, sql_query: str, outlet_id: str) -> Tuple[bool, str]:
        """
        Validate the SQL query for safety.
        
        Args:
            sql_query: The SQL query to validate
            outlet_id: The outlet_id that should be in the query
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Convert to uppercase for checking
        query_upper = sql_query.upper()
        
        # Check for forbidden keywords
        for keyword in FORBIDDEN_KEYWORDS:
            # Use word boundary matching
            pattern = r'\b' + keyword + r'\b'
            if re.search(pattern, query_upper):
                return False, f"Forbidden operation detected: {keyword}"
        
        # Ensure it's a SELECT query
        if not query_upper.strip().startswith("SELECT"):
            return False, "Only SELECT queries are allowed"
        
        # Check that outlet_id is present in the query for data isolation
        if outlet_id not in sql_query:
            return False, "Query must filter by outlet_id for data isolation"
        
        # Check for allowed tables only
        # Extract table names from the query (basic extraction)
        table_pattern = r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        tables_in_query = re.findall(table_pattern, sql_query, re.IGNORECASE)
        
        for table in tables_in_query:
            if table.lower() not in ALLOWED_TABLES:
                return False, f"Access to table '{table}' is not allowed"
        
        return True, ""
    
    async def execute_query(
        self,
        sql_query: str,
        outlet_id: str
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        Execute a validated SQL query.
        
        Args:
            sql_query: The SQL query to execute
            outlet_id: The outlet_id for validation
            
        Returns:
            Tuple of (results, error_message)
        """
        # Validate the query first
        is_valid, error_msg = self.validate_query(sql_query, outlet_id)
        if not is_valid:
            logger.warning(f"Query validation failed: {error_msg}")
            return None, error_msg
        
        try:
            # Execute using Supabase's RPC for raw SQL
            # Note: This requires the sql function to be set up in Supabase
            # Alternative: Use PostgREST for table-specific queries
            
            # For complex queries, we'll use the Supabase REST API
            # to execute raw SQL through a stored function
            result = self.client.rpc(
                "execute_sql",
                {"query_text": sql_query}
            ).execute()
            
            return result.data, None
            
        except Exception as e:
            error_str = str(e)
            logger.error(f"Query execution error: {error_str}")
            
            # Try alternative approach using PostgREST if RPC fails
            try:
                return await self._execute_with_postgrest(sql_query, outlet_id)
            except Exception as postgrest_error:
                logger.error(f"PostgREST fallback failed: {postgrest_error}")
                return None, f"Query execution failed: {error_str}"
    
    async def _execute_with_postgrest(
        self,
        sql_query: str,
        outlet_id: str
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        Execute queries using PostgREST API as fallback.
        This parses the SQL and converts to PostgREST calls with better date handling.
        """
        try:
            # Try to identify the main table
            table_match = re.search(
                r'\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)',
                sql_query,
                re.IGNORECASE
            )
            
            if not table_match:
                return None, "Could not identify table from query"
            
            table_name = table_match.group(1).lower()
            
            if table_name not in ALLOWED_TABLES:
                return None, f"Table '{table_name}' is not allowed"
            
            # Build the query
            query = self.client.table(table_name).select("*")
            query = query.eq("outlet_id", outlet_id)
            
            # Try to extract date filters from the SQL query
            # Look for date patterns like 'YYYY-MM-DD' or date comparisons
            date_patterns = [
                # Match: created_at >= 'YYYY-MM-DD' or created_at::date >= 'YYYY-MM-DD'
                (r"created_at(?:::date)?\s*>=\s*'(\d{4}-\d{2}-\d{2})'", "gte", "created_at"),
                (r"created_at(?:::date)?\s*>\s*'(\d{4}-\d{2}-\d{2})'", "gt", "created_at"),
                (r"created_at(?:::date)?\s*<=\s*'(\d{4}-\d{2}-\d{2})'", "lte", "created_at"),
                (r"created_at(?:::date)?\s*<\s*'(\d{4}-\d{2}-\d{2})'", "lt", "created_at"),
                (r"created_at(?:::date)?\s*=\s*'(\d{4}-\d{2}-\d{2})'", "eq", "created_at"),
                # Match: date >= 'YYYY-MM-DD' for ledger table
                (r"(?<![a-z_])date\s*>=\s*'(\d{4}-\d{2}-\d{2})'", "gte", "date"),
                (r"(?<![a-z_])date\s*<=\s*'(\d{4}-\d{2}-\d{2})'", "lte", "date"),
                (r"(?<![a-z_])date\s*=\s*'(\d{4}-\d{2}-\d{2})'", "eq", "date"),
            ]
            
            for pattern, operator, column in date_patterns:
                match = re.search(pattern, sql_query, re.IGNORECASE)
                if match:
                    date_value = match.group(1)
                    logger.info(f"Extracted date filter: {column} {operator} {date_value}")
                    if operator == "gte":
                        query = query.gte(column, date_value)
                    elif operator == "gt":
                        query = query.gt(column, date_value)
                    elif operator == "lte":
                        # Include the full day - use start of next day
                        try:
                            date_obj = datetime.strptime(date_value, "%Y-%m-%d")
                            next_day = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
                            query = query.lt(column, next_day)
                        except:
                            query = query.lte(column, date_value)
                    elif operator == "lt":
                        query = query.lt(column, date_value)
                    elif operator == "eq":
                        # For date equality, use range (start of day to start of next day)
                        try:
                            date_obj = datetime.strptime(date_value, "%Y-%m-%d")
                            next_day = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
                            query = query.gte(column, date_value)
                            query = query.lt(column, next_day)
                            logger.info(f"Date equality: {date_value} to {next_day}")
                        except Exception as e:
                            logger.error(f"Error parsing date {date_value}: {e}")
                            query = query.gte(column, date_value)
                            query = query.lte(column, date_value)
            
            # Try to extract status filter
            if "status" in sql_query.lower():
                # Check for status != 'Cancelled' pattern
                status_neq_match = re.search(r"status\s*(?:!=|<>)\s*'([^']+)'", sql_query, re.IGNORECASE)
                if status_neq_match:
                    excluded_status = status_neq_match.group(1)
                    query = query.neq("status", excluded_status)
                    logger.info(f"Applied status filter: neq {excluded_status}")
                
                # Check for status = 'Value' pattern
                status_eq_match = re.search(r"status\s*=\s*'([^']+)'", sql_query, re.IGNORECASE)
                if status_eq_match:
                    included_status = status_eq_match.group(1)
                    query = query.eq("status", included_status)
                    logger.info(f"Applied status filter: eq {included_status}")
            
            # Try to extract LIMIT
            limit_match = re.search(r'\bLIMIT\s+(\d+)', sql_query, re.IGNORECASE)
            if limit_match:
                limit_value = int(limit_match.group(1))
                query = query.limit(min(limit_value, 500))  # Cap at 500 for safety
                logger.info(f"Applied LIMIT: {limit_value}")
            else:
                query = query.limit(500)  # Default limit
            
            # Try to extract ORDER BY
            order_match = re.search(r'\bORDER\s+BY\s+(\w+)(?:\s+(ASC|DESC))?', sql_query, re.IGNORECASE)
            if order_match:
                order_column = order_match.group(1)
                order_direction = order_match.group(2)
                is_desc = order_direction and order_direction.upper() == "DESC"
                query = query.order(order_column, desc=is_desc)
                logger.info(f"Applied ORDER BY: {order_column} {'DESC' if is_desc else 'ASC'}")
            
            result = query.execute()
            logger.info(f"PostgREST fallback returned {len(result.data) if result.data else 0} rows")
            return result.data, None
            
        except Exception as e:
            logger.error(f"PostgREST fallback error: {e}")
            return None, str(e)
    
    async def get_sales_from_ledger(
        self,
        outlet_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get sales/revenue summary from the LEDGER table.
        This is the primary source for daily sales data.
        
        Args:
            outlet_id: The outlet ID
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            
        Returns:
            Sales summary from ledger
        """
        try:
            query = self.client.table("ledger") \
                .select("*") \
                .eq("outlet_id", outlet_id)
            
            if start_date:
                query = query.gte("date", start_date)
            if end_date:
                query = query.lt("date", end_date)
            
            result = query.execute()
            
            if not result.data:
                return {
                    "total_sales": 0,
                    "total_revenue": 0,
                    "total_expenses": 0,
                    "net_profit": 0,
                    "transaction_count": 0
                }
            
            # Calculate metrics from ledger entries
            total_revenue = 0
            total_expenses = 0
            
            for entry in result.data:
                amount = entry.get("amount", 0) or 0
                entry_type = (entry.get("type", "") or "").lower()
                
                if entry_type in ["credit", "revenue", "sales", "income"]:
                    total_revenue += amount
                elif entry_type in ["debit", "expense", "cost"]:
                    total_expenses += amount
            
            return {
                "total_sales": round(total_revenue, 2),
                "total_revenue": round(total_revenue, 2),
                "total_expenses": round(total_expenses, 2),
                "net_profit": round(total_revenue - total_expenses, 2),
                "transaction_count": len(result.data)
            }
            
        except Exception as e:
            logger.error(f"Error fetching sales from ledger: {e}")
            return {}
    
    async def get_order_summary(
        self,
        outlet_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get order/item summary from the ORDER_ITEMS_NEW table.
        Use this for item-wise breakdown and order counts.
        
        Args:
            outlet_id: The outlet ID
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            
        Returns:
            Order summary dictionary
        """
        try:
            query = self.client.table("order_items_new") \
                .select("*") \
                .eq("outlet_id", outlet_id) \
                .neq("status", "Cancelled")
            
            if start_date:
                query = query.gte("created_at", start_date)
            if end_date:
                query = query.lt("created_at", end_date)
            
            result = query.execute()
            
            if not result.data:
                return {
                    "total_orders": 0,
                    "total_items_sold": 0,
                    "total_amount": 0,
                    "average_order_value": 0
                }
            
            # Calculate metrics
            total_amount = sum(item.get("amount", 0) or 0 for item in result.data)
            unique_orders = len(set(item.get("order_num") for item in result.data))
            total_items = sum(item.get("quantity", 0) or 0 for item in result.data)
            avg_order_value = total_amount / unique_orders if unique_orders > 0 else 0
            
            return {
                "total_orders": unique_orders,
                "total_items_sold": total_items,
                "total_amount": round(total_amount, 2),
                "average_order_value": round(avg_order_value, 2)
            }
            
        except Exception as e:
            logger.error(f"Error fetching order summary: {e}")
            return {}
    
    async def get_sales_summary(
        self,
        outlet_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get combined sales summary - tries ledger first, falls back to orders.
        
        Args:
            outlet_id: The outlet ID
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            
        Returns:
            Combined sales summary dictionary
        """
        try:
            # First try to get sales from ledger (primary source)
            ledger_data = await self.get_sales_from_ledger(outlet_id, start_date, end_date)
            
            # Also get order data for additional metrics
            order_data = await self.get_order_summary(outlet_id, start_date, end_date)
            
            # Use ledger for sales/revenue, orders for item counts
            total_sales = ledger_data.get("total_sales", 0)
            
            # If ledger has no data, fall back to order amounts
            if total_sales == 0:
                total_sales = order_data.get("total_amount", 0)
            
            return {
                "total_sales": total_sales,
                "total_revenue": ledger_data.get("total_revenue", total_sales),
                "total_expenses": ledger_data.get("total_expenses", 0),
                "net_profit": ledger_data.get("net_profit", total_sales),
                "total_orders": order_data.get("total_orders", 0),
                "average_order_value": order_data.get("average_order_value", 0),
                "items_sold": order_data.get("total_items_sold", 0)
            }
            
        except Exception as e:
            logger.error(f"Error fetching sales summary: {e}")
            return {}
    
    async def get_top_items(
        self,
        outlet_id: str,
        limit: int = 5,
        start_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get top selling items for an outlet.
        
        Args:
            outlet_id: The outlet ID
            limit: Number of top items to return
            start_date: Start date filter
            
        Returns:
            List of top items with sales data
        """
        try:
            query = self.client.table("order_items_new") \
                .select("item_name, quantity, amount") \
                .eq("outlet_id", outlet_id) \
                .neq("status", "Cancelled")
            
            if start_date:
                query = query.gte("created_at", start_date)
            
            result = query.execute()
            
            if not result.data:
                return []
            
            # Aggregate by item name
            item_totals: Dict[str, Dict[str, Any]] = {}
            for row in result.data:
                item_name = row.get("item_name", "Unknown")
                if item_name not in item_totals:
                    item_totals[item_name] = {
                        "item_name": item_name,
                        "total_quantity": 0,
                        "total_amount": 0
                    }
                item_totals[item_name]["total_quantity"] += row.get("quantity", 0) or 0
                item_totals[item_name]["total_amount"] += row.get("amount", 0) or 0
            
            # Sort by total amount and return top items
            sorted_items = sorted(
                item_totals.values(),
                key=lambda x: x["total_amount"],
                reverse=True
            )
            
            return sorted_items[:limit]
            
        except Exception as e:
            logger.error(f"Error fetching top items: {e}")
            return []
    
    async def get_ledger_summary(
        self,
        outlet_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get ledger/financial summary for an outlet.
        
        Args:
            outlet_id: The outlet ID
            start_date: Start date filter
            end_date: End date filter
            
        Returns:
            Financial summary dictionary
        """
        try:
            query = self.client.table("ledger") \
                .select("*") \
                .eq("outlet_id", outlet_id)
            
            if start_date:
                query = query.gte("date", start_date)
            if end_date:
                query = query.lte("date", end_date)
            
            result = query.execute()
            
            if not result.data:
                return {
                    "total_revenue": 0,
                    "total_expenses": 0,
                    "net_profit": 0
                }
            
            total_revenue = 0
            total_expenses = 0
            
            for entry in result.data:
                amount = entry.get("amount", 0) or 0
                entry_type = (entry.get("type", "") or "").lower()
                
                if entry_type in ["credit", "revenue"]:
                    total_revenue += amount
                elif entry_type in ["debit", "expense"]:
                    total_expenses += amount
            
            return {
                "total_revenue": round(total_revenue, 2),
                "total_expenses": round(total_expenses, 2),
                "net_profit": round(total_revenue - total_expenses, 2)
            }
            
        except Exception as e:
            logger.error(f"Error fetching ledger summary: {e}")
            return {}


# Singleton instance - lazy initialization
_db_client: Optional[DatabaseClient] = None


def get_db_client() -> DatabaseClient:
    """Get the database client instance with lazy initialization."""
    global _db_client
    if _db_client is None:
        _db_client = DatabaseClient()
    return _db_client
