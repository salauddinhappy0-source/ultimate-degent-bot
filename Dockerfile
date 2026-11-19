# Ultimate Crypto (BAGENT claim bot) — Railway Dockerfile
# Python + Node.js 22 + Binance Agentic Wallet CLI (baw)
FROM python:3.11-slim

# Node.js 22 install (baw er jonno)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Binance Agentic Wallet CLI
RUN npm install -g @binance/agentic-wallet@1.12.0

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "bot.py"]
