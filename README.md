# Flight Search API

Serviço de busca de voos que consome diretamente a API interna do Google Flights via engenharia reversa — sem scraping, sem API key do Google, sem custo.

## Como funciona

O Google Flights expõe um endpoint interno que o próprio site usa no browser. O `fli` faz chamadas diretas a esse endpoint usando `curl_cffi` com `impersonate="chrome"` para simular um browser real e evitar bloqueios.

A nossa API recebe uma requisição com origem, destino e datas, e busca automaticamente os voos para **±1 dia** em cada data informada, retornando os resultados ordenados por preço.

## Endpoints

### `GET /health`
Verifica se o serviço está no ar. Não requer autenticação.

### `POST /search`
Busca voos de ida e (opcionalmente) volta. Requer o header `x-api-key`.

**Body:**
```json
{
  "from": "NAT",
  "to": "MVD",
  "departDate": "2026-11-25",
  "returnDate": "2026-11-30"
}
```

**O que é buscado:**
- Voos de ida para: 24/11, 25/11 e 26/11
- Voos de volta para: 29/11, 30/11 e 01/12

**Parâmetros opcionais:**

| Campo | Valores | Padrão |
|-------|---------|--------|
| `cabinClass` | `ECONOMY`, `PREMIUM_ECONOMY`, `BUSINESS`, `FIRST` | `ECONOMY` |
| `maxStops` | `ANY`, `NON_STOP`, `ONE_STOP_OR_FEWER` | `ANY` |
| `passengers` | número inteiro | `1` |

## Autenticação

Todas as chamadas ao `/search` precisam do header `x-api-key`. A chave é configurada via variável de ambiente `APIKEY` no `.env`.

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "x-api-key: SUA_CHAVE" \
  -d '{"from": "NAT", "to": "MVD", "departDate": "2026-11-25"}'
```

## Rodando localmente

```bash
pip install -e .
pip install fastapi "uvicorn[standard]" python-dotenv

python flight_api.py
```

Documentação interativa disponível em `http://localhost:8000/docs`.

## Variáveis de ambiente

| Variável | Descrição |
|----------|-----------|
| `APIKEY` | Chave de autenticação da API |
| `PORT` | Porta do servidor (padrão: `8000`) |

## Limitações

- O Google pode bloquear requisições em excesso (HTTP 429). Evite chamadas em sequência rápida.
- Por ser uma API interna não oficial, pode quebrar se o Google alterar o formato do endpoint.
