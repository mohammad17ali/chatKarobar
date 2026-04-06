const logger = require('./logger');
const { generateResponse } = require('./responseEngine');
const { phoneFromMessageKey } = require('./lidResolver');

function registerMessageHandler(sock) {
    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        for (const msg of messages) {
            try {
                if (!msg.message) continue;
                if (msg.key.fromMe) continue;
                if (msg.key.remoteJid === 'status@broadcast') continue;

                const sender = msg.key.remoteJid;
                const text =
                    msg.message.conversation ||
                    msg.message.extendedTextMessage?.text ||
                    '';

                const phone = phoneFromMessageKey(msg.key);

                logger.info(
                    { sender, phone, text, type, keyFields: Object.keys(msg.key) },
                    'Incoming message'
                );

                if (!text) {
                    logger.debug({ sender }, 'Non-text message received, skipping reply');
                    continue;
                }

                await sock.presenceSubscribe(sender);
                await sock.sendPresenceUpdate('composing', sender);

                const reply = await generateResponse(text, phone, sender);

                await sock.sendPresenceUpdate('paused', sender);
                await sock.sendMessage(sender, { text: reply });
                logger.info({ sender, phone }, 'Reply sent');
            } catch (err) {
                logger.error({ err, msgKey: msg.key }, 'Failed to process message');

                try {
                    await sock.sendPresenceUpdate('paused', msg.key.remoteJid);
                } catch (_) { /* best-effort cleanup */ }
            }
        }
    });

    logger.info('Message handler registered');
}

module.exports = { registerMessageHandler };
