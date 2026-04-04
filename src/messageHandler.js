const logger = require('./logger');
const { generateResponse } = require('./responseEngine');

/**
 * Bind message listeners to a Baileys socket.
 * Filters out status broadcasts, own messages, and protocol messages,
 * then sends back the response from the engine.
 */
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

                logger.info({ sender, text, type }, 'Incoming message');

                if (!text) {
                    logger.debug({ sender }, 'Non-text message received, skipping reply');
                    continue;
                }

                const reply = await generateResponse(text, sender);

                await sock.sendMessage(sender, { text: reply });
                logger.info({ sender, reply }, 'Reply sent');
            } catch (err) {
                logger.error({ err, msgKey: msg.key }, 'Failed to process message');
            }
        }
    });

    logger.info('Message handler registered');
}

module.exports = { registerMessageHandler };
