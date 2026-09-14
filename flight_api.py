#!/usr/bin/env python3
"""API de busca de voos usando FastAPI.

Endpoints:
- GET /health - Verifica se o serviço está funcionando
- POST /search - Busca voos de ida e volta com flexibilidade de datas
"""

import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

from fli.models import (
    Airline,
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchDates, SearchFlights

from agent_scan import scan_travel
from gemini_agent import extract_policy, gemini_configured, parse_airlines_text

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
    return_from: Optional[str] = Field(
        None, alias="returnFrom", description="Origem da volta (open-jaw)"
    )
    return_to: Optional[str] = Field(
        None, alias="returnTo", description="Destino da volta (open-jaw)"
    )
    cabin_class: str = Field(
        "ECONOMY", description="Classe da cabine (ECONOMY, BUSINESS, FIRST)"
    )
    max_stops: str = Field("ANY", description="Máximo de paradas (ANY, NON_STOP, ONE_STOP_OR_FEWER)")
    return_max_stops: Optional[str] = Field(
        None, alias="returnMaxStops", description="Máximo de paradas só na volta"
    )
    passengers: int = Field(1, description="Número de passageiros")
    airlines: Optional[list[str]] = Field(None, description="Filtro de companhias (IATA)")


class OneWayRequest(BaseModel):
    """Busca de um único trecho."""

    from_airport: str = Field(..., alias="from")
    to_airport: str = Field(..., alias="to")
    date: str
    cabin_class: str = Field("ECONOMY", alias="cabinClass")
    max_stops: str = Field("ANY", alias="maxStops")
    passengers: int = 1
    airlines: Optional[list[str]] = None


class CalendarRequest(BaseModel):
    """Busca de preços por data."""

    from_airport: str = Field(..., alias="from")
    to_airport: str = Field(..., alias="to")
    from_date: str = Field(..., alias="fromDate")
    to_date: str = Field(..., alias="toDate")
    cabin_class: str = Field("ECONOMY", alias="cabinClass")
    max_stops: str = Field("ANY", alias="maxStops")
    passengers: int = 1
    airlines: Optional[list[str]] = None


class ExtractPolicyRequest(BaseModel):
    """Extrai regras do contexto da viagem."""

    context: str = ""
    travel: dict


class ScanTravelRequest(BaseModel):
    """Roda o monitor de uma viagem."""

    travel: dict
    previous_best_price: Optional[float] = None


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
        "0": MaxStops.NON_STOP,
        "DIRETO": MaxStops.NON_STOP,
        "ONE_STOP": MaxStops.ONE_STOP_OR_FEWER,
        "ONE_STOP_OR_FEWER": MaxStops.ONE_STOP_OR_FEWER,
        "1": MaxStops.ONE_STOP_OR_FEWER,
        "TWO_PLUS_STOPS": MaxStops.TWO_OR_FEWER_STOPS,
        "TWO_OR_FEWER_STOPS": MaxStops.TWO_OR_FEWER_STOPS,
        "2": MaxStops.TWO_OR_FEWER_STOPS,
    }
    return mapping.get(max_stops.upper(), MaxStops.ANY)


def parse_airlines(codes: Optional[list[str]]) -> Optional[list[Airline]]:
    """Converte códigos IATA em enums Airline."""
    if not codes:
        return None
    airlines = []
    for code in parse_airlines_text(codes):
        try:
            airlines.append(getattr(Airline, code))
        except AttributeError:
            continue
    return airlines or None


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
    airlines: Optional[list[Airline]] = None,
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
            airlines=airlines,
        )

        search = SearchFlights()
        flights = search.search(filters, top_n=top_n)

        if not flights:
            return []

        return [format_flight_response(flight) for flight in flights]

    except Exception as e:
        print(f"Erro ao buscar voos para {date}: {e}")
        return []


def validate_date(date: str) -> str:
    """Valida o formato da data e retorna a própria data."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
        return date
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Data inválida: '{date}'. Use o formato YYYY-MM-DD.",
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
    Busca voos de ida e volta nas datas exatas informadas.

    Busca voos de ida para: departDate
    Busca voos de volta para: returnDate (se fornecido)
    Preços retornados em BRL (mercado brasileiro).
    """
    # Valida aeroportos
    origin = parse_airport(request.from_airport)
    destination = parse_airport(request.to_airport)

    # Valida parâmetros
    seat_type = parse_seat_type(request.cabin_class)
    max_stops = parse_max_stops(request.max_stops)

    # Valida datas
    depart_date = validate_date(request.depart_date)
    return_date = validate_date(request.return_date) if request.return_date else None

    print(f"\n🔍 Buscando voos {request.from_airport} → {request.to_airport}")
    print(f"📅 Data de ida: {depart_date}")
    if return_date:
        print(f"📅 Data de volta: {return_date}")

    # Busca voos de ida
    print(f"\n🛫 Buscando voos de ida para {depart_date}...")
    outbound_flights = search_flights_for_date(
        origin=origin,
        destination=destination,
        date=depart_date,
        seat_type=seat_type,
        max_stops=max_stops,
        passengers=request.passengers,
        top_n=5,
    )
    outbound_results = [SearchResultResponse(date=depart_date, flights=outbound_flights)]
    print(f"   ✅ Encontrados {len(outbound_flights)} voos")

    # Busca voos de volta (se fornecido)
    return_results = None
    if return_date:
        return_origin = parse_airport(request.return_from) if request.return_from else destination
        return_destination = parse_airport(request.return_to) if request.return_to else origin
        return_stops = (
            parse_max_stops(request.return_max_stops)
            if request.return_max_stops
            else max_stops
        )
        return_airlines = parse_airlines(request.airlines) if request.return_from else None
        print(f"\n🛬 Buscando voos de volta para {return_date}...")
        print(f"   {request.return_from or request.to_airport} → {request.return_to or request.from_airport}")
        return_flights = search_flights_for_date(
            origin=return_origin,
            destination=return_destination,
            date=return_date,
            seat_type=seat_type,
            max_stops=return_stops,
            passengers=request.passengers,
            top_n=5,
            airlines=return_airlines,
        )
        return_results = [SearchResultResponse(date=return_date, flights=return_flights)]
        print(f"   ✅ Encontrados {len(return_flights)} voos")

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
            "returnFrom": request.return_from,
            "returnTo": request.return_to,
            "cabinClass": request.cabin_class,
            "maxStops": request.max_stops,
            "returnMaxStops": request.return_max_stops,
            "passengers": request.passengers,
        },
        total_results=total_results,
    )


@app.post("/search/one-way")
async def search_one_way_endpoint(request: OneWayRequest, _: None = Security(verify_api_key)):
    """Busca um único trecho (ida ou volta)."""
    origin = parse_airport(request.from_airport)
    destination = parse_airport(request.to_airport)
    date = validate_date(request.date)
    flights = search_flights_for_date(
        origin=origin,
        destination=destination,
        date=date,
        seat_type=parse_seat_type(request.cabin_class),
        max_stops=parse_max_stops(request.max_stops),
        passengers=request.passengers,
        airlines=parse_airlines(request.airlines),
        top_n=8,
    )
    return {
        "date": date,
        "flights": flights,
        "search_params": request.model_dump(by_alias=True),
        "total_results": len(flights),
    }


@app.post("/search/calendar")
async def search_calendar_endpoint(request: CalendarRequest, _: None = Security(verify_api_key)):
    """Varre preços por data em um período (one-way)."""
    origin = parse_airport(request.from_airport)
    destination = parse_airport(request.to_airport)
    from_date = validate_date(request.from_date)
    to_date = validate_date(request.to_date)
    try:
        filters = DateSearchFilters(
            trip_type=TripType.ONE_WAY,
            passenger_info=PassengerInfo(adults=request.passengers),
            flight_segments=[
                FlightSegment(
                    departure_airport=[[origin, 0]],
                    arrival_airport=[[destination, 0]],
                    travel_date=from_date,
                )
            ],
            seat_type=parse_seat_type(request.cabin_class),
            stops=parse_max_stops(request.max_stops),
            airlines=parse_airlines(request.airlines),
            from_date=from_date,
            to_date=to_date,
        )
        results = SearchDates().search(filters) or []
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no calendário: {exc}") from exc

    dates = []
    for item in results:
        raw_date = item.date[0] if isinstance(item.date, tuple) else item.date
        dates.append(
            {
                "date": raw_date.strftime("%Y-%m-%d"),
                "price": item.price,
                "currency": item.currency,
            }
        )
    return {"dates": dates, "total_results": len(dates)}


@app.get("/agent/status")
async def agent_status():
    """Indica se o Gemini está configurado. Sem autenticação (só boolean)."""
    return {
        "gemini_configured": gemini_configured(),
        "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    }


@app.post("/agent/extract-policy")
async def agent_extract_policy(request: ExtractPolicyRequest, _: None = Security(verify_api_key)):
    """Lê o contexto da viagem e devolve regras estruturadas."""
    policy = extract_policy(request.context, request.travel)
    return {"policy": policy, "gemini_configured": gemini_configured()}


@app.post("/agent/scan")
async def agent_scan(request: ScanTravelRequest, _: None = Security(verify_api_key)):
    """Calendário + buscas one-way + score + briefing Gemini."""
    try:
        return scan_travel(request.travel, request.previous_best_price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha no monitor: {exc}") from exc


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
