FROM python:3.12-slim AS base

# Build tools are needed on some platforms (notably ARM/Oracle Cloud) where
# shap/scikit-learn don't always ship a prebuilt wheel and fall back to
# compiling from source. Removed in the same layer via apt cache cleanup
# to keep the final image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Create the runtime user before copying application files so Docker can set
# ownership during COPY instead of recursively walking the whole application
# tree in a separate layer.
RUN useradd --create-home --shell /bin/bash aitos

COPY --chown=aitos:aitos aitos/ ./aitos/
COPY --chown=aitos:aitos run_paper_trading.py run_live_trading.py run_continual_learning.py ./

# Non-root user -- the app has no business running as root, and root
# inside a container is one less thing to worry about if anything in the
# dependency chain is ever compromised.
USER aitos

# Both entrypoint scripts' health servers listen on one of these, depending
# on which script docker-compose actually runs for a given service.
EXPOSE 8090 8091

# No CMD here on purpose -- docker-compose.yml's per-service `command:`
# picks run_paper_trading.py or run_continual_learning.py explicitly, so it's
# never ambiguous which one a given container is running.
