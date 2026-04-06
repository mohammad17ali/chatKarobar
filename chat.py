"""
Query Handler module.
Orchestrates the query processing flow from user input to formatted response.
"""

import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import json

from cachetools import TTLCache

from config import get_settings
from database import get_db_client, DatabaseClient
from llm_service import get_llm_service, LLMService, get_current_datetime_context
from models import ChatRequest, ChatResponseData, QueryContext

logger = logging.getLogger(__name__)


class ConversationManager:
    """Manages conversation history for context continuity."""
    
    def __init__(self, max_conversations: int = 1000, ttl: int = 3600):
        """
        Initialize the conversation manager.
        
        Args:
            max_conversations: Maximum number of conversations to cache
            ttl: Time-to-live for conversations in seconds
        """
        self._conversations: TTLCache = TTLCache(
            maxsize=max_conversations,
            ttl=ttl
        )
    
    def get_or_create_context(
        self,
        conversation_id: Optional[str],
        outlet_id: str,
        outlet_name: str,
        username: str
    ) -> QueryContext:
        """
        Get existing conversation context or create a new one.
        
        Args:
            conversation_id: Optional existing conversation ID
            outlet_id: The outlet ID
            outlet_name: The outlet name
            username: The user's name
            
        Returns:
            QueryContext object
        """
        if conversation_id and conversation_id in self._conversations:
            context = self._conversations[conversation_id]
            # Verify outlet matches for security
            if context.outlet_id == outlet_id:
                return context
        
        # Create new context
        new_id = str(uuid.uuid4())
        context = QueryContext(
            conversation_id=new_id,
            outlet_id=outlet_id,
            outlet_name=outlet_name,
            username=username
        )
        self._conversations[new_id] = context
        return context
    
    def update_context(self, context: QueryContext) -> None:
        """Update the conversation context in cache."""
        self._conversations[context.conversation_id] = context


class QueryCache:
    """Cache for query results to reduce LLM and database calls."""
    
    def __init__(self, ttl: int = 300, maxsize: int = 500):
        """
        Initialize the query cache.
        
        Args:
            ttl: Time-to-live for cache entries in seconds
            maxsize: Maximum cache size
        """
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.enabled = get_settings().enable_cache
    
    def _generate_key(self, outlet_id: str, query: str) -> str:
        """Generate a cache key from outlet ID and query."""
        normalized_query = query.lower().strip()
        key_string = f"{outlet_id}:{normalized_query}"
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get(self, outlet_id: str, query: str) -> Optional[Dict[str, Any]]:
        """Get cached result if available."""
        if not self.enabled:
            return None
        
        key = self._generate_key(outlet_id, query)
        result = self._cache.get(key)
        
        if result:
            logger.debug(f"Cache hit for query: {query[:50]}...")
        
        return result
    
    def set(self, outlet_id: str, query: str, result: Dict[str, Any]) -> None:
        """Cache a query result."""
        if not self.enabled:
            return
        
        key = self._generate_key(outlet_id, query)
        self._cache[key] = result
        logger.debug(f"Cached result for query: {query[:50]}...")


class QueryHandler:
    """Main handler for processing user queries."""
    
    def __init__(self):
        """Initialize the query handler with dependencies."""
        self.db_client: DatabaseClient = get_db_client()
        self.llm_service: LLMService = get_llm_service()
        self.conversation_manager = ConversationManager()
        self.query_cache = QueryCache()
        self.settings = get_settings()
    
    async def process_query(self, request: ChatRequest) -> ChatResponseData:
        """
        Process a chat request and return a response.
        
        Args:
            request: The chat request from the user
            
        Returns:
            ChatResponseData with the AI-generated response
        """
        start_time = time.time()
        
        try:
            # 1. Get outlet information
            outlet = await self.db_client.get_outlet_by_name(request.outlet_name)
            
            if not outlet:
                return ChatResponseData(
                    response=f"I couldn't find an outlet named '{request.outlet_name}'. Please check the outlet name and try again.",
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            outlet_id = outlet.get("outlet_id")
            
            # 2. Get or create conversation context
            context = self.conversation_manager.get_or_create_context(
                request.conversation_id,
                outlet_id,
                request.outlet_name,
                request.username
            )
            
            # 3. Check cache for similar queries
            cached_result = self.query_cache.get(outlet_id, request.query)
            if cached_result:
                return ChatResponseData(
                    response=cached_result.get("response", ""),
                    query_executed=cached_result.get("query_executed"),
                    data_summary=cached_result.get("data_summary"),
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            # 4. Classify the query
            classification = await self.llm_service.classify_query(request.query)
            query_type = classification.get("type", "data_query")
            
            logger.info(f"Query classified as: {query_type}")
            
            # 5. Handle based on query type
            if query_type == "greeting":
                response = await self.llm_service.handle_general_query(
                    request.query,
                    request.outlet_name,
                    request.username
                )
                logger.info(f"Response: {response}")
                return ChatResponseData(
                    response=response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            elif query_type == "help":
                response = self._generate_help_response(request.username)
                return ChatResponseData(
                    response=response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            elif query_type == "clarification_needed":
                response = await self.llm_service.handle_clarification(
                    request.query,
                    request.outlet_name,
                    request.username
                )
                return ChatResponseData(
                    response=response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            # 6. Process data query
            return await self._handle_data_query(
                request,
                outlet_id,
                context,
                start_time
            )
            
        except Exception as e:
            logger.error(f"Error processing query: {e}", exc_info=True)
            return ChatResponseData(
                response="I encountered an error while processing your request. Please try again or rephrase your question.",
                processing_time_ms=self._calculate_time(start_time)
            )
    
    async def _handle_data_query(
        self,
        request: ChatRequest,
        outlet_id: str,
        context: QueryContext,
        start_time: float
    ) -> ChatResponseData:
        """
        Handle queries that require database access.
        
        Args:
            request: The original request
            outlet_id: The outlet ID for filtering
            context: The conversation context
            start_time: Query start time for timing
            
        Returns:
            ChatResponseData with data-based response
        """
        # Add user message to context
        context.add_message("user", request.query)
        
        # Generate SQL query
        sql_query, error_or_response = await self.llm_service.generate_sql_query(
            request.query,
            outlet_id,
            request.outlet_name,
            request.username,
            context.get_recent_messages()
        )
        
        if not sql_query:
            # LLM returned a direct response or error
            if error_or_response:
                context.add_message("assistant", error_or_response)
                self.conversation_manager.update_context(context)
                return ChatResponseData(
                    response=error_or_response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            else:
                return ChatResponseData(
                    response="I couldn't generate a query for your request. Could you please rephrase your question?",
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
        
        # Execute the query
        results, error = await self.db_client.execute_query(sql_query, outlet_id)
        
        if error:
            logger.warning(f"Query execution error: {error}")
            
            # FALLBACK STRATEGY 1: Try using helper methods for common queries
            fallback_response = await self._try_fallback_query(
                request.query,
                outlet_id,
                request.outlet_name,
                request.username
            )
            
            if fallback_response:
                context.add_message("assistant", fallback_response)
                self.conversation_manager.update_context(context)
                return ChatResponseData(
                    response=fallback_response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            # FALLBACK STRATEGY 2: Fetch raw data and let LLM analyze it
            logger.info("Attempting LLM-based data analysis fallback...")
            llm_fallback_response = await self._try_llm_data_analysis_fallback(
                request.query,
                outlet_id,
                request.outlet_name,
                request.username,
                sql_query
            )
            
            if llm_fallback_response:
                context.add_message("assistant", llm_fallback_response)
                self.conversation_manager.update_context(context)
                return ChatResponseData(
                    response=llm_fallback_response,
                    conversation_id=context.conversation_id,
                    processing_time_ms=self._calculate_time(start_time)
                )
            
            # FALLBACK STRATEGY 3: Return a helpful error message with suggestions
            error_response = self._generate_helpful_error_response(request.query, error)
            return ChatResponseData(
                response=error_response,
                query_executed=sql_query if self.settings.debug else None,
                conversation_id=context.conversation_id,
                processing_time_ms=self._calculate_time(start_time)
            )
        
        # Format the response
        formatted_response = await self.llm_service.format_response(
            request.query,
            sql_query,
            results or [],
            request.outlet_name,
            request.username
        )
        
        # Generate data summary
        data_summary = self._generate_data_summary(results or [])
        
        # Update conversation context
        context.add_message("assistant", formatted_response)
        self.conversation_manager.update_context(context)
        
        # Cache the result
        cache_entry = {
            "response": formatted_response,
            "query_executed": sql_query,
            "data_summary": data_summary
        }
        self.query_cache.set(outlet_id, request.query, cache_entry)
        
        return ChatResponseData(
            response=formatted_response,
            query_executed=sql_query if self.settings.debug else None,
            data_summary=data_summary,
            conversation_id=context.conversation_id,
            processing_time_ms=self._calculate_time(start_time)
        )
    
    async def _try_fallback_query(
        self,
        user_query: str,
        outlet_id: str,
        outlet_name: str,
        username: str
    ) -> Optional[str]:
        """
        Try fallback queries using predefined helper methods.
        Enhanced with better date handling and markdown formatting.
        
        Args:
            user_query: The user's query
            outlet_id: The outlet ID
            outlet_name: The outlet name
            username: The user's name
            
        Returns:
            Formatted markdown response or None
        """
        query_lower = user_query.lower()
        now = datetime.now()
        
        try:
            # Determine date range from query
            start_date = None
            end_date = None
            period = "all time"
            
            if "today" in query_lower:
                start_date = now.strftime("%Y-%m-%d")
                end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                period = f"today ({now.strftime('%B %d, %Y')})"
            elif "yesterday" in query_lower:
                yesterday = now - timedelta(days=1)
                start_date = yesterday.strftime("%Y-%m-%d")
                end_date = now.strftime("%Y-%m-%d")
                period = f"yesterday ({yesterday.strftime('%B %d, %Y')})"
            elif "this week" in query_lower or "week" in query_lower:
                # Start from Monday of current week
                days_since_monday = now.weekday()
                week_start = now - timedelta(days=days_since_monday)
                start_date = week_start.strftime("%Y-%m-%d")
                period = f"this week ({week_start.strftime('%b %d')} - {now.strftime('%b %d')})"
            elif "last week" in query_lower:
                # Last week (Monday to Sunday)
                days_since_monday = now.weekday()
                this_week_start = now - timedelta(days=days_since_monday)
                last_week_start = this_week_start - timedelta(days=7)
                last_week_end = this_week_start - timedelta(days=1)
                start_date = last_week_start.strftime("%Y-%m-%d")
                end_date = this_week_start.strftime("%Y-%m-%d")
                period = f"last week ({last_week_start.strftime('%b %d')} - {last_week_end.strftime('%b %d')})"
            elif "this month" in query_lower or "month" in query_lower:
                start_date = now.replace(day=1).strftime("%Y-%m-%d")
                period = f"this month ({now.strftime('%B %Y')})"
            elif "last month" in query_lower:
                first_of_this_month = now.replace(day=1)
                last_month_end = first_of_this_month - timedelta(days=1)
                last_month_start = last_month_end.replace(day=1)
                start_date = last_month_start.strftime("%Y-%m-%d")
                end_date = first_of_this_month.strftime("%Y-%m-%d")
                period = f"last month ({last_month_start.strftime('%B %Y')})"
            
            # Sales queries
            if any(word in query_lower for word in ["sales", "revenue", "total", "how much"]):
                summary = await self.db_client.get_sales_summary(
                    outlet_id,
                    start_date=start_date,
                    end_date=end_date
                )
                
                if summary:
                    total_sales = summary.get('total_sales', 0)
                    total_orders = summary.get('total_orders', 0)
                    avg_value = summary.get('average_order_value', 0)
                    items_sold = summary.get('items_sold', 0)
                    
                    if total_sales == 0 and total_orders == 0:
                        return f"""**No sales recorded** for {period} at **{outlet_name}**.

This could mean:
- No orders were placed during this period
- All orders were cancelled
- Data might not be synced yet

💡 **Dig Deeper:**
→ Try checking a different time period?
→ What were sales for last week?"""
                    
                    return f"""**₹{total_sales:,.2f}** in total sales for {period} at **{outlet_name}**.

📊 **Quick Stats:**
- **Orders:** {total_orders}
- **Average Order Value:** ₹{avg_value:,.2f}
- **Items Sold:** {items_sold}

💡 **Dig Deeper:**
→ What were my top selling items?
→ How does this compare to last week?"""
            
            # Top items query
            if any(word in query_lower for word in ["top", "best", "popular", "selling", "item"]):
                limit = 5  # Default
                if "10" in query_lower:
                    limit = 10
                elif "3" in query_lower:
                    limit = 3
                    
                top_items = await self.db_client.get_top_items(outlet_id, limit=limit, start_date=start_date)
                
                if top_items:
                    total_revenue = sum(item['total_amount'] for item in top_items)
                    items_text = "\n".join([
                        f"{i+1}. **{item['item_name']}** — ₹{item['total_amount']:,.2f} ({item['total_quantity']} sold)"
                        for i, item in enumerate(top_items)
                    ])
                    
                    top_item = top_items[0]
                    top_percentage = (top_item['total_amount'] / total_revenue * 100) if total_revenue > 0 else 0
                    
                    return f"""**Top {len(top_items)} selling items** at **{outlet_name}**:

{items_text}

📈 **{top_item['item_name']}** leads with **{top_percentage:.1f}%** of this selection's revenue.

💡 **Dig Deeper:**
→ Which items haven't sold recently?
→ What's the profit margin on top items?"""
                else:
                    return f"""**No sales data found** for {period}.

💡 **Dig Deeper:**
→ Try a different time period?
→ What were total sales this month?"""
            
            # Ledger/expense queries
            if any(word in query_lower for word in ["expense", "profit", "loss", "ledger", "financial"]):
                ledger_summary = await self.db_client.get_ledger_summary(
                    outlet_id,
                    start_date=start_date,
                    end_date=end_date
                )
                
                if ledger_summary:
                    total_revenue = ledger_summary.get('total_revenue', 0)
                    total_expenses = ledger_summary.get('total_expenses', 0)
                    net_profit = ledger_summary.get('net_profit', 0)
                    
                    profit_emoji = "📈" if net_profit >= 0 else "📉"
                    profit_label = "Profit" if net_profit >= 0 else "Loss"
                    
                    return f"""**Financial Summary** for **{outlet_name}** ({period}):

💵 **Revenue:** ₹{total_revenue:,.2f}
💸 **Expenses:** ₹{total_expenses:,.2f}
{profit_emoji} **Net {profit_label}:** ₹{abs(net_profit):,.2f}

{"Strong performance! Consider reinvesting profits." if net_profit > 0 else "Review your expense categories to identify savings opportunities." if net_profit < 0 else "Breaking even - focus on increasing sales or reducing costs."}

💡 **Dig Deeper:**
→ What are my biggest expense categories?
→ How has profit trended over the last month?"""
            
        except Exception as e:
            logger.error(f"Fallback query error: {e}")
        
        return None
    
    async def _try_llm_data_analysis_fallback(
        self,
        user_query: str,
        outlet_id: str,
        outlet_name: str,
        username: str,
        failed_sql_query: str
    ) -> Optional[str]:
        """
        Fallback: Fetch raw data from relevant tables and let LLM analyze it.
        Used when SQL execution fails but we can still fetch raw data.
        
        Args:
            user_query: The user's original query
            outlet_id: The outlet ID
            outlet_name: The outlet name
            username: The user's name
            failed_sql_query: The SQL query that failed (for context)
            
        Returns:
            LLM-generated response or None
        """
        try:
            query_lower = user_query.lower()
            raw_data = {}
            
            # Determine which data to fetch based on the query type
            # SALES/REVENUE queries -> fetch from LEDGER table
            fetch_ledger = any(word in query_lower for word in [
                "sales", "revenue", "total", "earn", "income", "money",
                "expense", "profit", "loss", "ledger", "financial"
            ])
            
            # ITEM/ORDER queries -> fetch from ORDER_ITEMS_NEW table
            fetch_orders = any(word in query_lower for word in [
                "order", "sold", "item", "best", "top", "popular", 
                "dish", "menu", "quantity", "what sold", "breakdown"
            ])
            
            # Determine date filter
            start_date = None
            end_date = None
            now = datetime.now()
            
            if "today" in query_lower:
                start_date = now.strftime("%Y-%m-%d")
                end_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            elif "yesterday" in query_lower:
                yesterday = now - timedelta(days=1)
                start_date = yesterday.strftime("%Y-%m-%d")
                end_date = now.strftime("%Y-%m-%d")
            elif "week" in query_lower:
                start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            elif "month" in query_lower:
                start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            
            # Fetch relevant data
            if fetch_orders:
                try:
                    query = self.db_client.client.table("order_items_new") \
                        .select("*") \
                        .eq("outlet_id", outlet_id) \
                        .neq("status", "Cancelled") \
                        .order("created_at", desc=True) \
                        .limit(500)
                    
                    if start_date:
                        query = query.gte("created_at", start_date)
                    if end_date:
                        query = query.lt("created_at", end_date)
                    
                    result = query.execute()
                    raw_data["order_items"] = result.data if result.data else []
                    logger.info(f"Fetched {len(raw_data['order_items'])} order items for fallback analysis")
                except Exception as e:
                    logger.error(f"Error fetching order data for fallback: {e}")
            
            if fetch_ledger:
                try:
                    query = self.db_client.client.table("ledger") \
                        .select("*") \
                        .eq("outlet_id", outlet_id) \
                        .order("date", desc=True) \
                        .limit(200)
                    
                    if start_date:
                        query = query.gte("date", start_date)
                    if end_date:
                        query = query.lt("date", end_date)
                    
                    result = query.execute()
                    raw_data["ledger"] = result.data if result.data else []
                    logger.info(f"Fetched {len(raw_data['ledger'])} ledger entries for fallback analysis")
                except Exception as e:
                    logger.error(f"Error fetching ledger data for fallback: {e}")
            
            # If we have no data, return None
            total_records = sum(len(v) for v in raw_data.values() if isinstance(v, list))
            if total_records == 0:
                logger.info("No data found for LLM fallback analysis")
                return None
            
            # Use LLM to analyze the raw data
            datetime_context = get_current_datetime_context()
            
            analysis_prompt = f"""
{datetime_context}

**CONTEXT**
User: {username} | Restaurant: {outlet_name}
Original Question: "{user_query}"

**DATA SOURCES:**
- `ledger` data = Daily SALES/REVENUE records. Sum 'amount' where type='credit'/'revenue' for total sales.
- `order_items` data = Individual ORDER LINE ITEMS. Use for item-wise breakdown, order counts, what sold.

**RAW DATA:**
```json
{json.dumps(raw_data, indent=2, default=str)[:8000]}
```

**YOUR TASK:**
1. Analyze this raw data to answer the user's question
2. For SALES questions: Use ledger data, sum amounts where type is 'credit'/'revenue'/'sales'
3. For ITEM questions: Use order_items data, aggregate by item_name
4. Calculate any sums, averages, or aggregations needed
5. Provide a clear, markdown-formatted response

**IMPORTANT:**
- Calculate totals manually from the data provided
- Format currency as ₹ with Indian comma format
- Output in well-formatted Markdown
- Be concise but informative
- End with 2 relevant follow-up questions
"""
            
            try:
                response = await self.llm_service._openai_client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=[
                        {
                            "role": "system",
                            "content": """You are a sharp business analyst for Indian restaurant owners.
You analyze raw data to provide insights. Output in pure Markdown format.
When calculating totals from order_items data, sum the 'amount' field.
Use ₹ for currency with Indian comma formatting (₹1,00,000).
Be concise, lead with the answer, and end with follow-up questions."""
                        },
                        {"role": "user", "content": analysis_prompt}
                    ],
                    max_tokens=1500,
                    temperature=0.5
                )
                
                return response.choices[0].message.content
                
            except Exception as e:
                logger.error(f"Error in LLM fallback analysis: {e}")
                return None
                
        except Exception as e:
            logger.error(f"Error in LLM data analysis fallback: {e}")
            return None
    
    def _generate_helpful_error_response(self, user_query: str, error: str) -> str:
        """
        Generate a helpful error response with suggestions for the user.
        
        Args:
            user_query: The original user query
            error: The error message
            
        Returns:
            User-friendly error message with suggestions
        """
        query_lower = user_query.lower()
        
        # Provide context-specific suggestions
        suggestions = []
        
        if any(word in query_lower for word in ["sales", "revenue", "sold"]):
            suggestions = [
                "What were my total sales today?",
                "Show me sales for this week",
                "What are my top selling items?"
            ]
        elif any(word in query_lower for word in ["order", "orders"]):
            suggestions = [
                "How many orders did I get today?",
                "Show me today's orders",
                "What's my average order value?"
            ]
        elif any(word in query_lower for word in ["item", "menu", "product"]):
            suggestions = [
                "What are my top 5 selling items?",
                "Which items sold the most this week?",
                "Show me item-wise sales breakdown"
            ]
        elif any(word in query_lower for word in ["expense", "profit", "ledger"]):
            suggestions = [
                "What are my total expenses this month?",
                "Show me my profit and loss",
                "What's my financial summary?"
            ]
        else:
            suggestions = [
                "What were my total sales today?",
                "Show me my top selling items",
                "How is my business performing this week?"
            ]
        
        suggestions_text = "\n".join([f"• {s}" for s in suggestions])
        
        return f"""I had trouble processing that specific query. Here are some alternative questions you can try:

{suggestions_text}

**Tip:** For best results, ask about specific time periods (today, yesterday, this week) or specific metrics (sales, orders, items).

💡 **Try asking:**
→ A simpler version of your question?
→ About a specific date range?"""
    
    def _generate_data_summary(
        self,
        results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Generate a structured summary of query results.
        
        Args:
            results: The query results
            
        Returns:
            Dictionary with summary statistics
        """
        if not results:
            return {"row_count": 0}
        
        summary = {
            "row_count": len(results)
        }
        
        # Try to extract numeric summaries
        if results:
            first_row = results[0]
            for key, value in first_row.items():
                if isinstance(value, (int, float)):
                    values = [r.get(key) for r in results if r.get(key) is not None]
                    if values:
                        summary[f"{key}_sum"] = sum(values)
                        summary[f"{key}_avg"] = sum(values) / len(values)
        
        return summary
    
    def _generate_help_response(self, username: str) -> str:
        """Generate a help response listing capabilities."""
        return f"""Hello {username}! 👋 I'm your Karobar AI assistant. Here's what I can help you with:

📊 **Sales Analysis**
• "What were my total sales today/yesterday/this week?"
• "Show me my revenue trend for the last 30 days"
• "How many orders did I receive today?"

🍽️ **Menu Insights**
• "What are my top 5 selling items?"
• "Which items haven't been ordered recently?"
• "Show me items by category"

💰 **Financial Queries**
• "What are my total expenses this month?"
• "Show me my profit and loss"
• "What's my daily revenue average?"

📈 **Business Summary**
• "Give me a business summary for this week"
• "How is my restaurant performing?"

Just ask me anything about your business data, and I'll help you find the answers! 🚀"""
    
    def _calculate_time(self, start_time: float) -> int:
        """Calculate processing time in milliseconds."""
        return int((time.time() - start_time) * 1000)


# Singleton instance
_query_handler: Optional[QueryHandler] = None


def get_query_handler() -> QueryHandler:
    """Get the query handler singleton instance."""
    global _query_handler
    if _query_handler is None:
        _query_handler = QueryHandler()
    return _query_handler
