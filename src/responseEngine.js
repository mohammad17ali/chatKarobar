const OpenAI = require('openai');
const logger = require('./logger');
const db = require('./database');

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
const model = process.env.OPENAI_MODEL || 'gpt-4o-mini';

const MAX_WORDS = 100;
const TOO_LONG_MSG = 'That message is a bit long for me to process. Could you send a shorter query? Try to keep it under a few sentences.';

// ---------------------------------------------------------------------------
// Phone result cache  (Map + manual TTL, avoids Supabase hit on every message)
// ---------------------------------------------------------------------------

const PHONE_CACHE_TTL_MS = 5 * 60 * 1000;
const phoneCache = new Map();

function getCachedPhoneResult(phone) {
    const entry = phoneCache.get(phone);
    if (!entry) return null;
    if (Date.now() > entry.expiresAt) {
        phoneCache.delete(phone);
        return null;
    }
    return entry.result;
}

function setCachedPhoneResult(phone, result) {
    phoneCache.set(phone, { result, expiresAt: Date.now() + PHONE_CACHE_TTL_MS });
}

// ---------------------------------------------------------------------------
// Phone check  (cache-first, then Supabase)
// ---------------------------------------------------------------------------

async function checkPhone(phone) {
    if (!phone) return { registered: false, user: null };

    const cached = getCachedPhoneResult(phone);
    if (cached) {
        logger.debug({ phone, cached: true }, 'Phone result from cache');
        return cached;
    }

    let result;
    try {
        const user = await db.getUserByPhone(phone);
        if (user && user.outlet_id) {
            logger.info(
                { phone, outletId: user.outlet_id, outletName: user.outlet_name },
                'Registered user identified'
            );
            result = { registered: true, user };
        } else {
            logger.info({ phone }, 'Phone not registered or no outlet linked');
            result = { registered: false, user: null };
        }
    } catch (err) {
        logger.error({ err, phone }, 'Phone check failed');
        result = { registered: false, user: null };
    }

    setCachedPhoneResult(phone, result);
    return result;
}

// ---------------------------------------------------------------------------
// System prompts
// ---------------------------------------------------------------------------

const REGISTERED_SYSTEM_PROMPT = `You are the Karobar AI assistant on WhatsApp — a sharp, friendly business analyst for Indian restaurant owners.

RULES:
- You ONLY answer questions related to Karobar and the user's restaurant business.
- Personalisation: You know the customer's name and their outlet (restaurant) name. Use them naturally — e.g. greet by first name when appropriate, and refer to their outlet by name when giving numbers or tips ("at *Your Outlet*", "for *Spice Kitchen*"). Do not overuse names; one or two touches per reply is enough.
- If the user sends a greeting, greet them back warmly by name and suggest what they can ask (sales, items, expenses).
- If the user asks for help, list your capabilities briefly.
- If the user asks anything unrelated to Karobar or their restaurant, politely decline in one line and redirect.
- For data questions, use the provided tools to fetch real data before answering.

Formatting (WhatsApp-compatible):
- *bold* for emphasis (single asterisk)
- _italic_ for subtle notes
- Numbered lists and bullet points
- Currency in ₹ with Indian comma format (₹1,00,000)
- Keep responses concise — this is WhatsApp, not an essay
- End data responses with 1-2 follow-up suggestions prefixed with →
- Use emojis to make responses engaging`;

const WELCOME_SYSTEM_PROMPT = `You are the Karobar welcome assistant on WhatsApp.
The person messaging is NOT a registered Karobar user.

Karobar (https://mykarobar.in) is a restaurant intelligence platform that helps restaurant owners:
- Track daily sales and revenue in real time
- Analyse top-selling menu items
- Monitor expenses and profit/loss
- Get AI-powered business insights on WhatsApp

YOUR JOB:
- Answer questions about Karobar's features, pricing, and benefits — warmly and briefly.
- Gently encourage them to sign up at https://mykarobar.in
- If they greet you, greet back and introduce Karobar in 2-3 lines.
- If they ask something unrelated to Karobar, politely decline in one line.
- NEVER make up business data. You have NO access to any database.
- Keep every response SHORT (3-5 lines max). This is WhatsApp.

Formatting: *bold* for emphasis, emojis for warmth, bullet points for lists.`;

// ---------------------------------------------------------------------------
// OpenAI tool definitions (registered agent only)
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
// Registered user agent  (DB tools + full Karobar knowledge)
// ---------------------------------------------------------------------------

async function handleRegisteredUser(text, user) {
    const today = new Date().toISOString().slice(0, 10);
    const userName = user.name || user.username || 'there';
    const firstName = userName.split(/\s+/)[0] || 'there';
    const outletName = user.outlet_name || 'your restaurant';
    const outletId = user.outlet_id;

    const messages = [
        {
            role: 'system',
            content:
                REGISTERED_SYSTEM_PROMPT +
                `\n\nToday's date: ${today}` +
                `\nCustomer name: ${userName}` +
                `\nFirst name (for greetings): ${firstName}` +
                `\nOutlet / restaurant name: ${outletName}`,
        },
        { role: 'user', content: text },
    ];

    // Step 1 — let OpenAI decide: answer directly or call a tool
    const first = await openai.chat.completions.create({
        model,
        messages,
        tools: DATA_TOOLS,
        tool_choice: 'auto',
        max_tokens: 400,
        temperature: 0.3,
    });

    const assistantMsg = first.choices[0]?.message;

    // No tool calls → greeting / help / off-topic / general answer
    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
        return assistantMsg.content?.trim() || 'How can I help with your restaurant today?';
    }

    // Step 2 — execute tools
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

    // Step 3 — format the final response
    const final = await openai.chat.completions.create({
        model,
        messages,
        max_tokens: 800,
        temperature: 0.4,
    });

    return final.choices[0]?.message?.content?.trim() || 'I retrieved your data but had trouble formatting it. Please try again.';
}

// ---------------------------------------------------------------------------
// Unregistered user agent  (no DB tools, Karobar promo only)
// ---------------------------------------------------------------------------

async function handleUnregisteredUser(text) {
    const res = await openai.chat.completions.create({
        model,
        messages: [
            { role: 'system', content: WELCOME_SYSTEM_PROMPT },
            { role: 'user', content: text },
        ],
        max_tokens: 250,
        temperature: 0.6,
    });

    return res.choices[0]?.message?.content?.trim() || (
        'Welcome! 👋 Karobar helps restaurant owners track sales and grow their business.\n\n' +
        'Sign up at https://mykarobar.in to get started!'
    );
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

async function generateResponse(incomingText, phone, senderJid) {
    logger.info({ phone, senderJid }, 'Processing query');

    // 1. Word count gate
    const wordCount = incomingText.trim().split(/\s+/).length;
    if (wordCount > MAX_WORDS) {
        logger.info({ wordCount }, 'Message too long, rejecting');
        return TOO_LONG_MSG;
    }

    // 2. Phone resolution guard
    if (!phone) {
        logger.warn({ senderJid }, 'Could not resolve phone number from message');
        return 'Sorry, I could not identify your phone number. Please ensure your WhatsApp number is linked correctly.';
    }

    // 3. Phone check — registered or not
    const { registered, user } = await checkPhone(phone);

    // 4. Route to the right agent
    if (registered) {
        return handleRegisteredUser(incomingText, user);
    }
    return handleUnregisteredUser(incomingText);
}

module.exports = { generateResponse };
