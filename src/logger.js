const pino = require('pino');
const path = require('path');
const fs = require('fs');

const logsDir = path.join(__dirname, '..', 'logs');
if (!fs.existsSync(logsDir)) {
    fs.mkdirSync(logsDir, { recursive: true });
}

const logLevel = process.env.LOG_LEVEL || 'info';

const logger = pino({
    level: logLevel,
    transport: {
        targets: [
            {
                target: 'pino-pretty',
                options: {
                    colorize: true,
                    translateTime: 'SYS:yyyy-mm-dd HH:MM:ss',
                    ignore: 'pid,hostname',
                },
                level: logLevel,
            },
            {
                target: 'pino/file',
                options: {
                    destination: path.join(logsDir, 'app.log'),
                    mkdir: true,
                },
                level: logLevel,
            },
        ],
    },
});

module.exports = logger;
