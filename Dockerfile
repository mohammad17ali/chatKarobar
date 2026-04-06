# chatKarobar — WhatsApp agent (Baileys + OpenAI + Supabase)
FROM node:22-alpine

WORKDIR /app

# Install dependencies first (better layer cache)
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY src ./src

# Run as non-root
RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001 -G nodejs \
    && mkdir -p /app/auth_info /app/logs \
    && chown -R nodejs:nodejs /app

USER nodejs

ENV NODE_ENV=production

CMD ["node", "src/index.js"]
