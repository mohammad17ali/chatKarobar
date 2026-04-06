const logger = require('./logger');

/**
 * Maps WhatsApp LIDs (Linked IDs) to real phone numbers.
 *
 * WhatsApp's multi-device protocol uses opaque LIDs internally.
 * This cache accumulates LID→phone mappings from multiple sources:
 *   - msg.key.senderPn / participantPn  (per-message attribute)
 *   - chats.phoneNumberShare events     (protocol-level share)
 */

const lidToPhone = new Map();

function register(lid, phoneJid) {
    if (!lid || !phoneJid) return;
    const lidUser = lid.split('@')[0];
    const phone = phoneJid.split('@')[0].replace(/[^0-9]/g, '');
    if (lidUser && phone) {
        lidToPhone.set(lidUser, phone);
        logger.debug({ lid: lidUser, phone }, 'LID→phone mapping registered');
    }
}

/**
 * Resolve a JID to a clean phone number string.
 * Handles both @s.whatsapp.net JIDs and @lid JIDs.
 */
function resolvePhone(jid) {
    if (!jid) return null;

    // Already a regular phone JID
    if (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@c.us')) {
        return jid.split('@')[0].replace(/[^0-9]/g, '');
    }

    // LID — look up in cache
    if (jid.endsWith('@lid')) {
        const lidUser = jid.split('@')[0];
        const cached = lidToPhone.get(lidUser);
        if (cached) return cached;

        logger.warn({ lid: lidUser }, 'No phone mapping found for LID');
        return null;
    }

    // Fallback: try to extract digits
    const digits = jid.split('@')[0].replace(/[^0-9]/g, '');
    return digits || null;
}

/**
 * Extract phone number from a Baileys message key using all available fields.
 * Returns the first resolved phone number it can find.
 */
function phoneFromMessageKey(key) {
    if (!key) return null;

    // 1. senderPn is the most direct source (phone JID from the stanza)
    if (key.senderPn) {
        const phone = key.senderPn.split('@')[0].replace(/[^0-9]/g, '');
        if (phone) {
            // Also register the mapping for future use
            if (key.remoteJid?.endsWith('@lid')) register(key.remoteJid, key.senderPn);
            return phone;
        }
    }

    // 2. participantPn (groups)
    if (key.participantPn) {
        const phone = key.participantPn.split('@')[0].replace(/[^0-9]/g, '');
        if (phone) {
            if (key.participant?.endsWith('@lid')) register(key.participant, key.participantPn);
            return phone;
        }
    }

    // 3. Resolve remoteJid through the cache or directly
    return resolvePhone(key.remoteJid);
}

module.exports = { register, resolvePhone, phoneFromMessageKey };
