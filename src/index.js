require('dotenv').config();

const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    Browsers,
} = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const qrcode = require('qrcode-terminal');

const logger = require('./logger');
const { registerMessageHandler } = require('./messageHandler');

const AUTH_DIR = './auth_info';

async function startSocket() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    const sock = makeWASocket({
        auth: state,
        logger: logger,
        browser: Browsers.ubuntu('chatKarobar'),
        printQRInTerminal: false,
        markOnlineOnConnect: false,
    });

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            logger.info('Scan the QR code below to authenticate:');
            qrcode.generate(qr, { small: true });
        }

        if (connection === 'close') {
            const statusCode =
                (lastDisconnect?.error)?.output?.statusCode;
            const loggedOut = statusCode === DisconnectReason.loggedOut;

            logger.warn(
                { statusCode, loggedOut },
                'Connection closed'
            );

            if (!loggedOut) {
                logger.info('Reconnecting...');
                startSocket();
            } else {
                logger.fatal('Logged out. Delete auth_info/ and re-scan QR to reconnect.');
            }
        }

        if (connection === 'open') {
            logger.info('Connected to WhatsApp successfully');
        }
    });

    sock.ev.on('creds.update', saveCreds);

    registerMessageHandler(sock);

    return sock;
}

startSocket().catch((err) => {
    logger.fatal({ err }, 'Failed to start chatKarobar');
    process.exit(1);
});
