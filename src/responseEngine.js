const OpenAI = require('openai');
const logger = require('./logger');
const db = require('./database');

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
const model = process.env.OPENAI_MODEL || 'gpt-4o-mini';

// ---------------------------------------------------------------------------
// System prompts
// ---------------------------------------------------------------------------

const CLASSIFIER_PROMPT = `You are a query classifier for Karobar, a restaurant intelligence platform.
Given a user message, respond with EXACTLY one JSON object (no markdown, no backticks):
{"type":"<TYPE>","reasoning":"<one line>"}

Types:
- "data_query"  → user wants business data (sales, revenue, orders, items, expenses, profit, financial summary, performance, etc.)
- "greeting"    → hello, hi, hey, good morning, etc.
- "help"        → user asks what you can do, or asks for help
- "general"     → question about Karobar the platform, its features, pricing, how to use it — no DB needed
- "off_topic"   → anything NOT related to Karobar or their restaurant business (weather, jokes, coding, politics, personal questions, etc.)`;

const RESPONSE_SYSTEM_PROMPT = `You are the Karobar AI assistant on WhatsApp — a sharp, friendly business analyst for Indian restaurant owners.
You ONLY answer questions related to Karobar and the user's restaurant business.
If the user asks anything unrelated, politely decline and redirect them to Karobar topics.

Formatting rules (WhatsApp-compatible):
- Use *bold* for emphasis (not **markdown bold**). Only one asterisk at each end of string.
- Use _italic_ for subtle notes
- Use numbered lists and bullet points
- Currency in ₹ with Indian comma format (₹1,00,000)
- Keep responses concise — this is WhatsApp, not an essay
- End data responses with 1-2 follow-up suggestions prefixed with →
- Use emojis for emphasis wherever relevant and make the response more engaging`;

const NOT_REGISTERED_MSG =
    `It looks like this phone number is not registered with Karobar.\n\n` +
    `Visit https://mykarobar.in to sign up and start tracking your restaurant's performance!\n\n` +
    `Once registered, you can ask me about your sales, top items, expenses, and more — right here on WhatsApp.`;

// ---------------------------------------------------------------------------
// OpenAI tool definitions (map to our database helpers)
// ---------------------------------------------------------------------------

const DATA_TOOLS = [
    {
        type: 'function',
        function: {
            name: 'get_sales_summary',
            description: 'Get total sales, revenue, orders count, average order value, and items sold for a time period',
            parameters: {
                type: 'object',
                properties: {
                    start_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                    end_date: { type: 'string', description: 'ISO date YYYY-MM-DD (exclusive upper bound)' },
                },
            },
        },
    },
    {
        type: 'function',
        function: {
            name: 'get_top_items',
            description: 'Get top selling menu items ranked by revenue',
            parameters: {
                type: 'object',
                properties: {
                    limit: { type: 'number', description: 'How many items to return (default 5)' },
                    start_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                },
            },
        },
    },
    {
        type: 'function',
        function: {
            name: 'get_ledger_summary',
            description: 'Get financial summary — total revenue, total expenses, net profit/loss',
            parameters: {
                type: 'object',
                properties: {
                    start_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                    end_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                },
            },
        },
    },
    {
        type: 'function',
        function: {
            name: 'get_order_summary',
            description: 'Get order statistics — total orders, items sold, total amount, average order value',
            parameters: {
                type: 'object',
                properties: {
                    start_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                    end_date: { type: 'string', description: 'ISO date YYYY-MM-DD' },
                },
            },
        },
    },
];

const TOOL_DISPATCH = {
    get_sales_summary: (outletId, args) =>
        db.getSalesSummary(outletId, args.start_date, args.end_date),
    get_top_items: (outletId, args) =>
        db.getTopItems(outletId, args.limit || 5, args.start_date),
    get_ledger_summary: (outletId, args) =>
        db.getLedgerSummary(outletId, args.start_date, args.end_date),
    get_order_summary: (outletId, args) =>
        db.getOrderSummary(outletId, args.start_date, args.end_date),
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function todayISO() {
    return new Date().toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

async function classifyQuery(text) {
    const res = await openai.chat.completions.create({
        model,
        messages: [
            { role: 'system', content: CLASSIFIER_PROMPT },
            { role: 'user', content: text },
        ],
        max_tokens: 100,
        temperature: 0,
    });

    const raw = (res.choices[0]?.message?.content || '').trim();
    try {
        return JSON.parse(raw);
    } catch {
        logger.warn({ raw }, 'Failed to parse classification JSON, defaulting to general');
        return { type: 'general', reasoning: 'parse failure' };
    }
}

// ---------------------------------------------------------------------------
// Non-data responses
// ---------------------------------------------------------------------------

async function handleGreeting(text, userName) {
    const res = await openai.chat.completions.create({
        model,
        messages: [
            { role: 'system', content: RESPONSE_SYSTEM_PROMPT },
            {
                role: 'user',
                content: `The user "${userName || 'there'}" just greeted with: "${text}". ` +
                    `Greet them back warmly, mention Karobar, and suggest what they can ask about (sales, items, expenses).` +
                    ` Keep it to 2-3 lines.`,
            },
        ],
        max_tokens: 200,
        temperature: 0.7,
    });
    return res.choices[0]?.message?.content?.trim() || 'Hello! How can I help with your restaurant today?';
}

function handleHelp(userName) {
    return (
        `Hello ${userName || 'there'}! 👋 I'm your *Karobar AI assistant*. Here's what I can help with:\n\n` +
        `📊 *Sales Analysis*\n` +
        `• "What were my total sales today?"\n` +
        `• "Show me this week's revenue"\n\n` +
        `🍽️ *Menu Insights*\n` +
        `• "What are my top 5 selling items?"\n` +
        `• "Which items sold the most this week?"\n\n` +
        `💰 *Financial Queries*\n` +
        `• "What are my expenses this month?"\n` +
        `• "Show me my profit and loss"\n\n` +
        `📈 *Order Stats*\n` +
        `• "How many orders did I get today?"\n` +
        `• "What's my average order value?"\n\n` +
        `Just ask me anything about your business data! 🚀`
    );
}

async function handleGeneral(text) {
    const res = await openai.chat.completions.create({
        model,
        messages: [
            {
                role: 'system',
                content:
                    RESPONSE_SYSTEM_PROMPT +
                    '\n\nKarobar is a restaurant intelligence platform (https://mykarobar.in) that helps restaurant owners ' +
                    'track sales, manage inventory, analyse menu performance, and make data-driven decisions. ' +
                    'Answer ONLY about Karobar. If the question is not about Karobar, politely decline.',
            },
            { role: 'user', content: text },
        ],
        max_tokens: 400,
        temperature: 0.5,
    });
    return res.choices[0]?.message?.content?.trim() || 'I can only help with Karobar-related questions.';
}

function handleOffTopic() {
    return (
        `I appreciate the question, but I can only assist with topics related to *Karobar* and your restaurant business.\n\n` +
        `Try asking about your sales, top-selling items, expenses, or orders — I'm here to help with that! 📊`
    );
}

// ---------------------------------------------------------------------------
// Data query handler (with OpenAI function-calling)
// ---------------------------------------------------------------------------

async function handleDataQuery(text, outletId, userName, outletName) {
    const today = todayISO();

    const messages = [
        {
            role: 'system',
            content:
                RESPONSE_SYSTEM_PROMPT +
                `\n\nToday's date: ${today}` +
                `\nUser: ${userName || 'Restaurant Owner'}` +
                `\nOutlet: ${outletName || 'their restaurant'}` +
                `\n\nYou have access to database tools. Call the appropriate tool(s) to fetch data, then compose a WhatsApp-friendly response.`,
        },
        { role: 'user', content: text },
    ];

    // Step 1: Let OpenAI decide which tool(s) to call
    const toolCall = await openai.chat.completions.create({
        model,
        messages,
        tools: DATA_TOOLS,
        tool_choice: 'auto',
        max_tokens: 300,
        temperature: 0,
    });

    const assistantMsg = toolCall.choices[0]?.message;

    // If OpenAI responded without calling a tool, return its answer directly
    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
        return assistantMsg.content?.trim() || 'I could not determine which data to fetch. Could you rephrase your question?';
    }

    // Step 2: Execute each requested tool
    messages.push(assistantMsg);

    for (const tc of assistantMsg.tool_calls) {
        const fn = TOOL_DISPATCH[tc.function.name];
        if (!fn) {
            messages.push({ role: 'tool', tool_call_id: tc.id, content: JSON.stringify({ error: 'Unknown function' }) });
            continue;
        }

        let args = {};
        try { args = JSON.parse(tc.function.arguments || '{}'); } catch { /* empty */ }

        logger.info({ tool: tc.function.name, args }, 'Executing data tool');
        const result = await fn(outletId, args);
        messages.push({ role: 'tool', tool_call_id: tc.id, content: JSON.stringify(result) });
    }

    // Step 3: Let OpenAI format the final response using the tool results
    const final = await openai.chat.completions.create({
        model,
        messages,
        max_tokens: 800,
        temperature: 0.4,
    });

    return final.choices[0]?.message?.content?.trim() || 'I retrieved your data but had trouble formatting it. Please try again.';
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

async function generateResponse(incomingText, phone, senderJid) {
    logger.info({ phone, senderJid }, 'Processing query');

    if (!phone) {
        logger.warn({ senderJid }, 'Could not resolve phone number from message');
        return 'Sorry, I could not identify your phone number. Please ensure your WhatsApp number is linked correctly.';
    }

    // 1. Classify
    const classification = await classifyQuery(incomingText);
    logger.info({ classification }, 'Query classified');

    // 2. Off-topic — reject immediately (no DB check needed)
    if (classification.type === 'off_topic') {
        return handleOffTopic();
    }

    // 3. Non-data routes that don't require user lookup
    if (classification.type === 'greeting') {
        // Try to get user name if possible, but don't block on it
        let userName;
        try {
            const user = await db.getUserByPhone(phone);
            userName = user?.name || user?.username;
        } catch { /* ignore */ }
        return handleGreeting(incomingText, userName);
    }

    if (classification.type === 'help') {
        let userName;
        try {
            const user = await db.getUserByPhone(phone);
            userName = user?.name || user?.username;
        } catch { /* ignore */ }
        return handleHelp(userName);
    }

    if (classification.type === 'general') {
        return handleGeneral(incomingText);
    }

    // 4. Data query — requires authenticated user
    const user = await db.getUserByPhone(phone);
    if (!user) {
        logger.info({ phone }, 'Unregistered user attempted data query');
        return NOT_REGISTERED_MSG;
    }

    const outletId = user.outlet_id;
    if (!outletId) {
        return 'Your account does not have an outlet linked. Please contact Karobar support or visit https://mykarobar.in for help.';
    }

    const userName = user.name || user.username || 'there';
    const outletName = user.outlet_name || '';

    return handleDataQuery(incomingText, outletId, userName, outletName);
}

module.exports = { generateResponse };
