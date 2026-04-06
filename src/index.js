require('dotenv').config();

const {
    default: makeWASocket,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    DisconnectReason,
    Browsers,
} = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');

const logger = require('./logger');
const { registerMessageHandler } = require('./messageHandler');
const { register: registerLidMapping } = require('./lidResolver');

const AUTH_DIR = './auth_info';

const MAX_RETRIES = 10;
const INITIAL_BACKOFF_MS = 2000;
const MAX_BACKOFF_MS = 60000;

let retryCount = 0;

function getBackoffMs() {
    const ms = Math.min(INITIAL_BACKOFF_MS * Math.pow(2, retryCount), MAX_BACKOFF_MS);
    return ms;
}

async function startSocket() {
    const { version, isLatest } = await fetchLatestBaileysVersion();
    logger.info({ version, isLatest }, 'Using WA Web version');

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    const sock = makeWASocket({
        auth: state,
        version,
        logger: logger,
        browser: Browsers.macOS('Desktop'),
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
            const statusCode = lastDisconnect?.error?.output?.statusCode;

            logger.warn({ statusCode }, 'Connection closed');

            if (statusCode === DisconnectReason.loggedOut) {
                logger.fatal('Logged out. Delete auth_info/ and re-scan QR to reconnect.');
                process.exit(1);
            }

            if (statusCode === DisconnectReason.forbidden) {
                logger.fatal('Connection forbidden (403). Your session may be banned or invalid.');
                process.exit(1);
            }

            if (retryCount >= MAX_RETRIES) {
                logger.fatal(
                    { retryCount },
                    'Max reconnect attempts reached. Exiting.'
                );
                process.exit(1);
            }

            const backoff = getBackoffMs();
            retryCount++;
            logger.info(
                { attempt: retryCount, maxRetries: MAX_RETRIES, backoffMs: backoff },
                'Scheduling reconnect...'
            );
            setTimeout(startSocket, backoff);
        }

        if (connection === 'open') {
            retryCount = 0;
            logger.info('Connected to WhatsApp successfully');
        }
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('chats.phoneNumberShare', ({ lid, jid }) => {
        logger.info({ lid, jid }, 'Phone number share received');
        registerLidMapping(lid, jid);
    });

    registerMessageHandler(sock);

    return sock;
}

startSocket().catch((err) => {
    logger.fatal({ err }, 'Failed to start chatKarobar');
    process.exit(1);
});
