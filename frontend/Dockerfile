FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Disabled telemetry for Next.js
ENV NEXT_TELEMETRY_DISABLED 1

CMD ["npm", "run", "dev"]
