/**
 * Response engine -- generates a reply for an incoming message.
 *
 * Currently returns a static test string.
 * Will be replaced with an OpenAI API call later.
 */

async function generateResponse(_incomingText, _senderJid) {
    return 'Hi, you are seeing a message being sent for testing purposes!';
}

module.exports = { generateResponse };
