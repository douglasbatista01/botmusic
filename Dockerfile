# Use uma imagem base oficial do Python
FROM python:3.10-slim

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
