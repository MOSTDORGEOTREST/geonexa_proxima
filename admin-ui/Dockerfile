# Сборка админки. Двухступенчатая: в финальный образ не попадают ни исходники,
# ни devDependencies — только собранный node-сервер и его runtime-зависимости.
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY svelte.config.js vite.config.ts tsconfig.json ./
COPY src ./src
COPY static ./static
RUN npm run build && npm prune --omit=dev

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production

# Не под root: у node-слоя есть доступ к внутреннему API, и лишние права ему
# здесь не нужны.
RUN addgroup -S proxima && adduser -S proxima -G proxima

COPY --from=build --chown=proxima:proxima /app/build ./build
COPY --from=build --chown=proxima:proxima /app/node_modules ./node_modules
COPY --from=build --chown=proxima:proxima /app/package.json ./package.json

USER proxima
EXPOSE 3000

# ORIGIN обязателен: без него SvelteKit отвергает POST-формы как CSRF.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "build/index.js"]
