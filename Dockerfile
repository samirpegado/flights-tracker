# Dockerfile otimizado para Coolify
FROM python:3.11-slim

# Variáveis de ambiente
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instalar dependências do sistema
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Criar diretório de trabalho
WORKDIR /app

# Copiar arquivos de dependências
COPY pyproject.toml README.md ./
COPY fli ./fli

# Instalar dependências Python
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir fastapi "uvicorn[standard]"

# Copiar código da aplicação
COPY flight_api.py ./

# Expor porta
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Comando para iniciar
CMD ["uvicorn", "flight_api:app", "--host", "0.0.0.0", "--port", "8000"]
