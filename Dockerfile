# Use uma imagem base oficial do Python
FROM python:3.10-slim

# [CORREÇÃO DEFINITIVA] Instala o FFmpeg, a ferramenta essencial para processamento de áudio.
# O -y confirma a instalação automaticamente.
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copia o arquivo de dependências primeiro para otimizar o cache do Docker
COPY requirements.txt .

# Instala as dependências do bot
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o resto do código do bot para o diretório de trabalho
COPY . .

# Comando que será executado para iniciar o bot
CMD ["python", "bot.py"]
