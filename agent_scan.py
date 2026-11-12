"""Varredura determinística: calendário → pares de datas → voos → score."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Callable

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

from flight_score import score_combo
from gemini_agent import extract_policy, parse_airlines_text, write_briefing

SEARCH_DELAY_SECONDS = 0.8
TOP_DATE_PAIRS = 6
TOP_FLIGHTS = 4


def _parse_airport(code: str) -> Airport:
    try:
        return getattr(Airport, code.upper())
    except AttributeError as exc:
        raise ValueError(f"Aeroporto '{code}' não encontrado") from exc


def _parse_seat(cabin_class: str) -> SeatType:
    mapping = {
        "ECONOMY": SeatType.ECONOMY,
        "PREMIUM_ECONOMY": SeatType.PREMIUM_ECONOMY,
        "BUSINESS": SeatType.BUSINESS,
        "FIRST": SeatType.FIRST,
    }
    return mapping.get((cabin_class or "ECONOMY").upper(), SeatType.ECONOMY)


def _parse_stops(max_stops: str | None) -> MaxStops:
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
    return mapping.get((max_stops or "ANY").upper(), MaxStops.ANY)


def _parse_airlines(codes: list[str]) -> list[Airline] | None:
    airlines: list[Airline] = []
    for code in codes:
        try:
            airlines.append(getattr(Airline, code.upper()))
        except AttributeError:
            continue
    return airlines or None


def _flight_to_dict(flight) -> dict[str, Any]:
    return {
        "price": flight.price,
        "currency": flight.currency,
        "duration_minutes": flight.duration,
        "stops": flight.stops,
        "legs": [
            {
                "airline": leg.airline.value,
                "flight_number": leg.flight_number,
                "departure_airport": leg.departure_airport.name,
                "arrival_airport": leg.arrival_airport.name,
                "departure_datetime": leg.departure_datetime.isoformat(),
                "arrival_datetime": leg.arrival_datetime.isoformat(),
                "duration_minutes": leg.duration,
            }
            for leg in flight.legs
        ],
    }


def search_one_way(
    origin: Airport,
    destination: Airport,
    date: str,
    seat_type: SeatType,
    max_stops: MaxStops,
    passengers: int,
    airlines: list[Airline] | None = None,
    max_duration: int | None = None,
    top_n: int = TOP_FLIGHTS,
) -> list[dict[str, Any]]:
    """Busca voos one-way em uma data."""
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
            max_duration=max_duration,
        )
        flights = SearchFlights().search(filters, top_n=top_n) or []
        return [_flight_to_dict(flight) for flight in flights]
    except Exception as exc:
        print(f"Erro one-way {origin}→{destination} {date}: {exc}")
        return []


def search_calendar(
    origin: Airport,
    destination: Airport,
    from_date: str,
    to_date: str,
    seat_type: SeatType,
    max_stops: MaxStops,
    passengers: int,
    airlines: list[Airline] | None = None,
) -> list[dict[str, Any]]:
    """Preços por data (calendário Google Flights)."""
    filters = DateSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=passengers),
        flight_segments=[
            FlightSegment(
                departure_airport=[[origin, 0]],
                arrival_airport=[[destination, 0]],
                travel_date=from_date,
            )
        ],
        seat_type=seat_type,
        stops=max_stops,
        airlines=airlines,
        from_date=from_date,
        to_date=to_date,
    )
    results = SearchDates().search(filters) or []
    dates: list[dict[str, Any]] = []
    for item in results:
        raw_date = item.date[0] if isinstance(item.date, tuple) else item.date
        dates.append(
            {
                "date": raw_date.strftime("%Y-%m-%d"),
                "price": item.price,
                "currency": item.currency,
            }
        )
    return dates


def _sample_dates(start: datetime, end: datetime, step: int | None = None) -> list[str]:
    span = max((end - start).days, 0)
    if step is None:
        step = 1 if span <= 10 else 2
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=step)
    if end.strftime("%Y-%m-%d") not in dates:
        dates.append(end.strftime("%Y-%m-%d"))
    return dates


def _duration_matches(nights: int, duration_min: int, duration_max: int) -> bool:
    """Aceita noites (volta − ida) ou dias corridos inclusivos (13 a 17 = 5 dias)."""
    if nights < 0:
        return False
    inclusive = nights + 1
    return (
        duration_min <= nights <= duration_max
        or duration_min <= inclusive <= duration_max
    )


def _valid_pairs(
    outbound_dates: list[str],
    return_dates: list[str],
    price_by_out: dict[str, float],
    price_by_ret: dict[str, float],
    duration_min: int,
    duration_max: int,
) -> list[tuple[str, str, float]]:
    pairs: list[tuple[str, str, float]] = []
    for depart in outbound_dates:
        d0 = datetime.strptime(depart, "%Y-%m-%d")
        for back in return_dates:
            d1 = datetime.strptime(back, "%Y-%m-%d")
            nights = (d1 - d0).days
            if _duration_matches(nights, duration_min, duration_max):
                total = price_by_out.get(depart, 9_999_999) + price_by_ret.get(back, 9_999_999)
                pairs.append((depart, back, total))
    if not pairs and outbound_dates and return_dates:
        first = min(outbound_dates)
        last = max(return_dates)
        if last >= first:
            total = price_by_out.get(first, 9_999_999) + price_by_ret.get(last, 9_999_999)
            pairs.append((first, last, total))
    pairs.sort(key=lambda item: item[2])
    return pairs[:TOP_DATE_PAIRS]


def scan_travel(
    travel: dict[str, Any],
    previous_best_price: float | None = None,
    search_one_way_fn: Callable[..., list[dict[str, Any]]] = search_one_way,
) -> dict[str, Any]:
    """Executa o monitor de uma viagem e devolve combinações + briefing."""
    policy = travel.get("extracted_policy") or extract_policy(
        travel.get("context_text") or "",
        travel,
    )

    origin = _parse_airport(travel["from"])
    destination = _parse_airport(travel["to"])
    return_from = _parse_airport(travel.get("return_from") or travel["to"])
    return_to = _parse_airport(travel.get("return_to") or travel["from"])

    passengers = int(travel.get("passengers") or 1)
    seat_type = _parse_seat(travel.get("cabin_class") or "ECONOMY")
    out_stops = _parse_stops(travel.get("outbound_max_stops") or travel.get("max_stops"))
    ret_stops = _parse_stops(travel.get("return_max_stops") or travel.get("max_stops"))
    preferred = parse_airlines_text(
        policy.get("preferred_airlines_return") or travel.get("preferred_airlines")
    )
    ret_airlines = _parse_airlines(preferred)
    max_duration = policy.get("outbound_hard_max_duration_minutes")

    duration_min = int(
        policy.get("duration_min_days")
        or travel.get("duration_min_days")
        or 1
    )
    duration_max = int(
        policy.get("duration_max_days")
        or travel.get("duration_max_days")
        or duration_min
    )

    window_start = travel.get("window_start")
    window_end = travel.get("window_end")
    dates_scanned = 0
    pairs: list[tuple[str, str, float]] = []

    if window_start and window_end:
        start = datetime.strptime(str(window_start), "%Y-%m-%d")
        end = datetime.strptime(str(window_end), "%Y-%m-%d")
        used_calendar = False
        try:
            out_cal = search_calendar(
                origin, destination, str(window_start), str(window_end),
                seat_type, out_stops, passengers,
            )
            time.sleep(SEARCH_DELAY_SECONDS)
            ret_cal = search_calendar(
                return_from, return_to, str(window_start), str(window_end),
                seat_type, ret_stops, passengers, ret_airlines,
            )
            if out_cal and ret_cal:
                out_dates = [row["date"] for row in out_cal]
                ret_dates = [row["date"] for row in ret_cal]
                pairs = _valid_pairs(
                    out_dates,
                    ret_dates,
                    {row["date"]: row["price"] for row in out_cal},
                    {row["date"]: row["price"] for row in ret_cal},
                    duration_min,
                    duration_max,
                )
                dates_scanned = len(set(out_dates + ret_dates))
                used_calendar = bool(pairs)
        except Exception as exc:
            print(f"Calendário falhou, usando amostragem: {exc}")
        if not used_calendar:
            print("Calendário vazio; amostrando datas da janela.")
            sampled = _sample_dates(start, end)
            pairs = _valid_pairs(
                sampled, sampled, {}, {}, duration_min, duration_max
            )
            dates_scanned = len(sampled)
    elif travel.get("depart_date"):
        depart = str(travel["depart_date"])
        back = str(travel.get("return_date") or travel["depart_date"])
        pairs = [(depart, back, 0)]
        dates_scanned = 2 if travel.get("return_date") else 1
    else:
        raise ValueError("Informe uma janela (window_start/window_end) ou data de ida.")

    combinations: list[dict[str, Any]] = []
    for depart, back, _estimate in pairs:
        time.sleep(SEARCH_DELAY_SECONDS)
        outbound_flights = search_one_way_fn(
            origin, destination, depart, seat_type, out_stops, passengers,
            None, max_duration,
        )
        return_flights: list[dict[str, Any]] = []
        if travel.get("return_from") or travel.get("return_date") or travel.get("return_to"):
            time.sleep(SEARCH_DELAY_SECONDS)
            return_flights = search_one_way_fn(
                return_from, return_to, back, seat_type, ret_stops, passengers,
                ret_airlines, None,
            )

        if not outbound_flights:
            continue

        if return_flights:
            for outbound in outbound_flights[:2]:
                for ret in return_flights[:2]:
                    score, reasons = score_combo(outbound, ret, policy)
                    combinations.append(
                        {
                            "outbound_date": depart,
                            "return_date": back,
                            "outbound": outbound,
                            "return": ret,
                            "total_price": round(
                                float(outbound["price"]) + float(ret["price"]), 2
                            ),
                            "score": score,
                            "score_reasons": reasons,
                        }
                    )
        else:
            outbound = outbound_flights[0]
            score, reasons = score_combo(outbound, None, policy)
            combinations.append(
                {
                    "outbound_date": depart,
                    "return_date": None,
                    "outbound": outbound,
                    "return": None,
                    "total_price": float(outbound["price"]),
                    "score": score,
                    "score_reasons": reasons,
                }
            )

    combinations.sort(key=lambda item: item["score"])
    top = combinations[:8]
    briefing = write_briefing(travel, policy, top, previous_best_price)
    best_price = top[0]["total_price"] if top else None
    improved = (
        previous_best_price is not None
        and best_price is not None
        and best_price < float(previous_best_price) - 1
    ) or (previous_best_price is None and best_price is not None)

    return {
        "extracted_policy": policy,
        "dates_scanned": dates_scanned,
        "combinations_found": len(combinations),
        "combinations": top,
        "briefing": briefing,
        "best_total_price": best_price,
        "best_score": top[0]["score"] if top else None,
        "improved": improved,
        "previous_best_price": previous_best_price,
    }
