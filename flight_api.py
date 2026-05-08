#!/usr/bin/env python3
"""API de busca de voos usando FastAPI.

Endpoints:
- GET /health - Verifica se o serviço está funcionando
- POST /search - Busca voos de ida e volta com flexibilidade de datas
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from fli.models import (
    Airport,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
)
from fli.search import SearchFlights

load_dotenv()

API_KEY = os.getenv("APIKEY")
api_key_header = APIKeyHeader(name="x-api-key")


# Modelos de dados
class SearchRequest(BaseModel):
    """Modelo de requisição de busca de voos."""

    from_airport: str = Field(..., alias="from", description="Código IATA do aeroporto de origem")
    to_airport: str = Field(..., alias="to", description="Código IATA do aeroporto de destino")
    depart_date: str = Field(
        ..., alias="departDate", description="Data de ida (YYYY-MM-DD)"
    )
    return_date: Optional[str] = Field(
        None, alias="returnDate", description="Data de volta (YYYY-MM-DD)"
    )
    cabin_class: str = Field(
        "ECONOMY", description="Classe da cabine (ECONOMY, BUSINESS, FIRST)"
    )
    max_stops: str = Field("ANY", description="Máximo de paradas (ANY, NON_STOP, ONE_STOP_OR_FEWER)")
    passengers: int = Field(1, description="Número de passageiros")


class FlightLegResponse(BaseModel):
    """Modelo de resposta para um trecho de voo."""

    airline: str
    flight_number: str
    departure_airport: str
    arrival_airport: str
    departure_datetime: str
    arrival_datetime: str
    duration_minutes: int


class FlightResponse(BaseModel):
    """Modelo de resposta para um voo completo."""

    price: float
    currency: Optional[str]
    duration_minutes: int
    stops: int
    legs: list[FlightLegResponse]


class SearchResultResponse(BaseModel):
    """Modelo de resposta para resultados de busca."""

    date: str
    flights: list[FlightResponse]


class SearchResponse(BaseModel):
    """Modelo de resposta completa da busca."""

    outbound_flights: list[SearchResultResponse]
    return_flights: Optional[list[SearchResultResponse]] = None
    search_params: dict
    total_results: int


class HealthResponse(BaseModel):
    """Modelo de resposta do health check."""

    status: str
    timestamp: str
    version: str


# Inicializa FastAPI
app = FastAPI(
    title="Flight Search API",
    description="API para busca de voos usando Google Flights",
    version="1.0.0",
)

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def verify_api_key(key: str = Security(api_key_header)) -> None:
    """Verifica se o x-api-key do header é válido."""
    if not API_KEY:
        raise HTTPException(status_code=500, detail="APIKEY não configurada no servidor.")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="x-api-key inválida.")


def parse_airport(code: str) -> Airport:
    """Converte código IATA para enum Airport."""
    try:
        return getattr(Airport, code.upper())
    except AttributeError:
        raise HTTPException(
            status_code=400,
            detail=f"Aeroporto '{code}' não encontrado. Use códigos IATA válidos (ex: NAT, MVD, GRU).",
        )


def parse_seat_type(cabin_class: str) -> SeatType:
    """Converte classe de cabine para enum SeatType."""
    mapping = {
        "ECONOMY": SeatType.ECONOMY,
        "PREMIUM_ECONOMY": SeatType.PREMIUM_ECONOMY,
        "BUSINESS": SeatType.BUSINESS,
        "FIRST": SeatType.FIRST,
    }
    return mapping.get(cabin_class.upper(), SeatType.ECONOMY)


def parse_max_stops(max_stops: str) -> MaxStops:
    """Converte máximo de paradas para enum MaxStops."""
    mapping = {
        "ANY": MaxStops.ANY,
        "NON_STOP": MaxStops.NON_STOP,
        "ONE_STOP": MaxStops.ONE_STOP_OR_FEWER,
        "ONE_STOP_OR_FEWER": MaxStops.ONE_STOP_OR_FEWER,
        "TWO_PLUS_STOPS": MaxStops.TWO_OR_FEWER_STOPS,
        "TWO_OR_FEWER_STOPS": MaxStops.TWO_OR_FEWER_STOPS,
    }
    return mapping.get(max_stops.upper(), MaxStops.ANY)


def format_flight_response(flight) -> FlightResponse:
    """Formata um voo para resposta da API."""
    return FlightResponse(
        price=flight.price,
        currency=flight.currency,
        duration_minutes=flight.duration,
        stops=flight.stops,
        legs=[
            FlightLegResponse(
                airline=leg.airline.value,
                flight_number=leg.flight_number,
                departure_airport=leg.departure_airport.value,
                arrival_airport=leg.arrival_airport.value,
                departure_datetime=leg.departure_datetime.isoformat(),
                arrival_datetime=leg.arrival_datetime.isoformat(),
                duration_minutes=leg.duration,
            )
            for leg in flight.legs
        ],
    )


def search_flights_for_date(
    origin: Airport,
    destination: Airport,
    date: str,
    seat_type: SeatType,
    max_stops: MaxStops,
    passengers: int,
    top_n: int = 5,
) -> list[FlightResponse]:
    """Busca voos para uma data específica."""
    try:
        filters = FlightSearchFilters(
            passenger_info=PassengerInfo(adults=passengers),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[origin, 0]],
                    arrival_airport=[[destination, 0]],
                    travel_date=date,
                )
            ],
            seat_type=seat_type,
            stops=max_stops,
            sort_by=SortBy.CHEAPEST,
        )

        search = SearchFlights()
        flights = search.search(filters, top_n=top_n)

        if not flights:
            return []

        return [format_flight_response(flight) for flight in flights]

    except Exception as e:
        print(f"Erro ao buscar voos para {date}: {e}")
        return []


def get_date_range(base_date: str) -> list[str]:
    """Retorna lista de datas: base_date-1, base_date, base_date+1."""
    try:
        base = datetime.strptime(base_date, "%Y-%m-%d")
        return [
            (base - timedelta(days=1)).strftime("%Y-%m-%d"),
            base.strftime("%Y-%m-%d"),
            (base + timedelta(days=1)).strftime("%Y-%m-%d"),
        ]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Data inválida: '{base_date}'. Use o formato YYYY-MM-DD.",
        )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Verifica se o serviço está funcionando."""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now().isoformat(),
        version="1.0.0",
    )


@app.post("/search", response_model=SearchResponse)
async def search_flights(request: SearchRequest, _: None = Security(verify_api_key)):
    """
    Busca voos de ida e volta com flexibilidade de datas.

    Busca voos de ida para: departDate-1, departDate, departDate+1
    Busca voos de volta para: returnDate-1, returnDate, returnDate+1 (se fornecido)
    """
    # Valida aeroportos
    origin = parse_airport(request.from_airport)
    destination = parse_airport(request.to_airport)

    # Valida parâmetros
    seat_type = parse_seat_type(request.cabin_class)
    max_stops = parse_max_stops(request.max_stops)

    # Gera datas de busca
    outbound_dates = get_date_range(request.depart_date)
    return_dates = get_date_range(request.return_date) if request.return_date else None

    print(f"\n🔍 Buscando voos {request.from_airport} → {request.to_airport}")
    print(f"📅 Datas de ida: {outbound_dates}")
    if return_dates:
        print(f"📅 Datas de volta: {return_dates}")

    # Busca voos de ida
    outbound_results = []
    for date in outbound_dates:
        print(f"\n🛫 Buscando voos de ida para {date}...")
        flights = search_flights_for_date(
            origin=origin,
            destination=destination,
            date=date,
            seat_type=seat_type,
            max_stops=max_stops,
            passengers=request.passengers,
            top_n=5,
        )
        outbound_results.append(SearchResultResponse(date=date, flights=flights))
        print(f"   ✅ Encontrados {len(flights)} voos")

    # Busca voos de volta (se fornecido)
    return_results = None
    if return_dates:
        return_results = []
        for date in return_dates:
            print(f"\n🛬 Buscando voos de volta para {date}...")
            flights = search_flights_for_date(
                origin=destination,
                destination=origin,
                date=date,
                seat_type=seat_type,
                max_stops=max_stops,
                passengers=request.passengers,
                top_n=5,
            )
            return_results.append(SearchResultResponse(date=date, flights=flights))
            print(f"   ✅ Encontrados {len(flights)} voos")

    # Calcula total de resultados
    total_outbound = sum(len(r.flights) for r in outbound_results)
    total_return = sum(len(r.flights) for r in return_results) if return_results else 0
    total_results = total_outbound + total_return

    print(f"\n✅ Busca concluída! Total: {total_results} voos encontrados")

    return SearchResponse(
        outbound_flights=outbound_results,
        return_flights=return_results,
        search_params={
            "from": request.from_airport,
            "to": request.to_airport,
            "departDate": request.depart_date,
            "returnDate": request.return_date,
            "cabinClass": request.cabin_class,
            "maxStops": request.max_stops,
            "passengers": request.passengers,
        },
        total_results=total_results,
    )


if __name__ == "__main__":
    import uvicorn
    import os

    # Porta configurável via variável de ambiente (para Coolify)
    port = int(os.getenv("PORT", 8000))

    print("🚀 Iniciando Flight Search API...")
    print(f"📍 Porta: {port}")
    print("📚 Documentação: /docs")
    print("🔍 Health check: /health")
    print("\n⏳ Aguardando requisições...\n")

    uvicorn.run(app, host="0.0.0.0", port=port)
