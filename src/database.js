const { createClient } = require('@supabase/supabase-js');
const logger = require('./logger');

const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_KEY;

if (!supabaseUrl || !supabaseKey) {
    logger.warn('SUPABASE_URL or SUPABASE_KEY not set -- database calls will fail');
}

const supabase = supabaseUrl && supabaseKey
    ? createClient(supabaseUrl, supabaseKey)
    : null;

function getClient() {
    if (!supabase) throw new Error('Supabase client not initialized (check .env)');
    return supabase;
}

// ---------------------------------------------------------------------------
// User lookup
// ---------------------------------------------------------------------------

async function getUserByPhone(phone) {
    const db = getClient();
    const cleaned = phone.replace(/[^0-9]/g, '');

    logger.info({ phone: cleaned }, 'Looking up user by phone');
    const { data, error } = await db
        .from('users')
        .select('*')
        .eq('phone', cleaned)
        .limit(1);

    if (error) {
        logger.error({ error }, 'Error looking up user by phone');
        return null;
    }

    if (data && data.length > 0) return data[0];

    // Fallback: try without country code prefix (first 2 digits)
    if (cleaned.length > 10) {
        const withoutCC = cleaned.slice(cleaned.length - 10);
        const { data: fallback, error: fbErr } = await db
            .from('users')
            .select('*')
            .eq('phone', withoutCC)
            .limit(1);

        if (fbErr) {
            logger.error({ error: fbErr }, 'Fallback phone lookup failed');
            return null;
        }
        if (fallback && fallback.length > 0) return fallback[0];
    }

    return null;
}

// ---------------------------------------------------------------------------
// Sales summary  (ledger + order_items_new, mirrors Python get_sales_summary)
// ---------------------------------------------------------------------------

async function getSalesSummary(outletId, startDate, endDate) {
    const ledger = await getSalesFromLedger(outletId, startDate, endDate);
    const orders = await getOrderSummary(outletId, startDate, endDate);

    let totalSales = ledger.totalSales || 0;
    if (totalSales === 0) totalSales = orders.totalAmount || 0;

    return {
        totalSales,
        totalRevenue: ledger.totalRevenue || totalSales,
        totalExpenses: ledger.totalExpenses || 0,
        netProfit: ledger.netProfit ?? totalSales,
        totalOrders: orders.totalOrders || 0,
        averageOrderValue: orders.averageOrderValue || 0,
        itemsSold: orders.totalItemsSold || 0,
    };
}

async function getSalesFromLedger(outletId, startDate, endDate) {
    const db = getClient();
    let query = db.from('ledger').select('*').eq('outlet_id', outletId);
    if (startDate) query = query.gte('date', startDate);
    if (endDate) query = query.lt('date', endDate);

    const { data, error } = await query;
    if (error) { logger.error({ error }, 'Ledger fetch error'); return {}; }
    if (!data || data.length === 0) {
        return { totalSales: 0, totalRevenue: 0, totalExpenses: 0, netProfit: 0, transactionCount: 0 };
    }

    let totalRevenue = 0, totalExpenses = 0;
    for (const entry of data) {
        const amount = entry.amount || 0;
        const type = (entry.type || '').toLowerCase();
        if (['credit', 'revenue', 'sales', 'income'].includes(type)) totalRevenue += amount;
        else if (['debit', 'expense', 'cost'].includes(type)) totalExpenses += amount;
    }

    return {
        totalSales: Math.round(totalRevenue * 100) / 100,
        totalRevenue: Math.round(totalRevenue * 100) / 100,
        totalExpenses: Math.round(totalExpenses * 100) / 100,
        netProfit: Math.round((totalRevenue - totalExpenses) * 100) / 100,
        transactionCount: data.length,
    };
}

// ---------------------------------------------------------------------------
// Order summary  (order_items_new)
// ---------------------------------------------------------------------------

async function getOrderSummary(outletId, startDate, endDate) {
    const db = getClient();
    let query = db.from('order_items_new').select('*')
        .eq('outlet_id', outletId)
        .neq('status', 'Cancelled');
    if (startDate) query = query.gte('created_at', startDate);
    if (endDate) query = query.lt('created_at', endDate);

    const { data, error } = await query;
    if (error) { logger.error({ error }, 'Order summary fetch error'); return {}; }
    if (!data || data.length === 0) {
        return { totalOrders: 0, totalItemsSold: 0, totalAmount: 0, averageOrderValue: 0 };
    }

    const totalAmount = data.reduce((s, r) => s + (r.amount || 0), 0);
    const uniqueOrders = new Set(data.map(r => r.order_num)).size;
    const totalItems = data.reduce((s, r) => s + (r.quantity || 0), 0);

    return {
        totalOrders: uniqueOrders,
        totalItemsSold: totalItems,
        totalAmount: Math.round(totalAmount * 100) / 100,
        averageOrderValue: uniqueOrders > 0 ? Math.round((totalAmount / uniqueOrders) * 100) / 100 : 0,
    };
}

// ---------------------------------------------------------------------------
// Top selling items
// ---------------------------------------------------------------------------

async function getTopItems(outletId, limit = 5, startDate) {
    const db = getClient();
    let query = db.from('order_items_new')
        .select('item_name, quantity, amount')
        .eq('outlet_id', outletId)
        .neq('status', 'Cancelled');
    if (startDate) query = query.gte('created_at', startDate);

    const { data, error } = await query;
    if (error) { logger.error({ error }, 'Top items fetch error'); return []; }
    if (!data || data.length === 0) return [];

    const map = {};
    for (const row of data) {
        const name = row.item_name || 'Unknown';
        if (!map[name]) map[name] = { item_name: name, total_quantity: 0, total_amount: 0 };
        map[name].total_quantity += row.quantity || 0;
        map[name].total_amount += row.amount || 0;
    }

    return Object.values(map)
        .sort((a, b) => b.total_amount - a.total_amount)
        .slice(0, limit);
}

// ---------------------------------------------------------------------------
// Ledger / financial summary
// ---------------------------------------------------------------------------

async function getLedgerSummary(outletId, startDate, endDate) {
    const db = getClient();
    let query = db.from('ledger').select('*').eq('outlet_id', outletId);
    if (startDate) query = query.gte('date', startDate);
    if (endDate) query = query.lt('date', endDate);

    const { data, error } = await query;
    if (error) { logger.error({ error }, 'Ledger summary fetch error'); return {}; }
    if (!data || data.length === 0) {
        return { totalRevenue: 0, totalExpenses: 0, netProfit: 0 };
    }

    let totalRevenue = 0, totalExpenses = 0;
    for (const entry of data) {
        const amount = entry.amount || 0;
        const type = (entry.type || '').toLowerCase();
        if (['credit', 'revenue'].includes(type)) totalRevenue += amount;
        else if (['debit', 'expense'].includes(type)) totalExpenses += amount;
    }

    return {
        totalRevenue: Math.round(totalRevenue * 100) / 100,
        totalExpenses: Math.round(totalExpenses * 100) / 100,
        netProfit: Math.round((totalRevenue - totalExpenses) * 100) / 100,
    };
}

module.exports = {
    getUserByPhone,
    getSalesSummary,
    getTopItems,
    getLedgerSummary,
    getOrderSummary,
};
