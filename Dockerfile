FROM node:20-bullseye

WORKDIR /app

# Gerekli sistem paketleri
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*
    
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
    
RUN ln -s /usr/bin/python3 /usr/bin/python || true
RUN ln -s /usr/bin/pip3 /usr/bin/pip || true

# Tüm projeyi kopyala
COPY . .

# Script executable
RUN chmod +x install.sh

# Install script çalıştır
RUN ./install.sh

# PATH fix (opencode için)
ENV PATH="/root/.opencode/bin:${PATH}"

CMD ["/bin/bash"]
