# main.py — проверка засухи по координатам и актуальным данным
import asyncio
import math
from datetime import datetime, date

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Смарт-контракт: засуха в Омской области")

# Пороги засухи
TEMP_MAX_THRESHOLD = 28.0
PRECIP_PER_DAY = 4.0

# Nominatim допускает не более одного запроса в секунду.
region_lock = asyncio.Lock()
region_cache = {}
region_last_request = 0.0


class ContractInput(BaseModel):
    latitude: float = 54.99
    longitude: float = 73.32
    radius_km: float = 50.0
    district: str = "Омский"
    insurance_sum: float = 1_000_000.0
    start_date: str = "2025-07-01"
    end_date: str = "2025-08-15"


# ========== РЕЕСТР ЗАСУХ ==========
HISTORICAL_DROUGHTS = [
    ("2025-06-01", "2025-08-31",
     "Почвенная засуха. Режим ЧС в центральных районах Омской области"),
    ("2012-06-15", "2012-11-07",
     "Аномальная засуха. Режим ЧС в 12 районах Омской области"),
    ("2010-06-15", "2010-08-20",
     "Сильная засуха. Потеря урожая до 40%"),
    ("2021-06-01", "2021-07-31",
     "Почвенная засуха. Режим ЧС в 6 районах"),
    ("2020-06-20", "2020-08-10",
     "Атмосферная засуха в южных районах"),
]

# ========== РЕЕСТР ПРАВОВЫХ АКТОВ О ЧС ==========
LEGAL_ACTS = [
    ("2025-06-20", "2025-09-15",
     "Постановление Правительства Омской области № 312-п о введении режима ЧС "
     "из-за почвенной засухи"),
    ("2012-07-21", "2012-11-07",
     "Указ Губернатора Омской области № 65 от 21.07.2012 о введении режима ЧС "
     "из-за атмосферной и почвенной засухи"),
    ("2021-06-25", "2021-08-31",
     "Распоряжение о введении режима ЧС в 6 районах"),
]


def build_bbox(lat, lon, radius_km):
    lat_d = radius_km / 111.0
    lon_d = radius_km / (111.0 * math.cos(math.radians(lat)))
    return f"{lon-lon_d:.4f},{lat-lat_d:.4f},{lon+lon_d:.4f},{lat+lat_d:.4f}"


async def is_inside_omsk_oblast(client, lat, lon):
    """Определяет регион центральной точки; None означает отсутствие данных."""
    global region_last_request
    key = (lat, lon)
    async with region_lock:
        if key in region_cache:
            return region_cache[key]
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0, 1.1 - (loop.time() - region_last_request)))
        region_last_request = loop.time()
        try:
            response = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2",
                        "zoom": 5, "addressdetails": 1},
                headers={"User-Agent": "DroughtContract/1.0 (regional eligibility check)"},
                timeout=10,
            )
            response.raise_for_status()
            address = response.json().get("address", {})
            region = address.get("ISO3166-2-lvl4")
            if region:
                result = region == "RU-OMS"
            elif address.get("country_code") not in (None, "ru"):
                result = False
            else:
                return None
            if len(region_cache) >= 1000:
                region_cache.clear()
            region_cache[key] = result
            return result
        except (httpx.HTTPError, ValueError, TypeError):
            return None


def days_in_period(start, end):
    try:
        sd = datetime.strptime(start, "%Y-%m-%d")
        ed = datetime.strptime(end, "%Y-%m-%d")
        return max((ed - sd).days + 1, 1)
    except ValueError:
        return 30


# ========== 1. ИСТОРИЧЕСКИЙ РЕЕСТР ==========
def fetch_historical_registry(start_date, end_date, in_omsk_oblast=None):
    if in_omsk_oblast is None:
        return {"status": "NO_DATA", "source": "Исторический реестр засух",
                "details": "Не удалось определить регион точки: омский реестр не применён"}
    if not in_omsk_oblast:
        return {"status": "NOT_APPLICABLE", "source": "Исторический реестр засух",
                "details": "Точка договора находится за пределами Омской области"}
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"status": "NO_DATA", "source": "Исторический реестр",
                "details": "Неверный формат даты"}

    for ds, de, desc in HISTORICAL_DROUGHTS:
        ds_dt = datetime.strptime(ds, "%Y-%m-%d")
        de_dt = datetime.strptime(de, "%Y-%m-%d")
        if sd <= de_dt and ed >= ds_dt:
            return {"status": "VERIFIED", "source": "Исторический реестр засух",
                    "details": f"Период совпадает с засухой {sd.year}: {desc}"}

    return {"status": "UNVERIFIED", "source": "Исторический реестр засух",
            "details": f"Засуха в {start_date} — {end_date} не зафиксирована"}


# ========== 2. РЕЕСТР ПРАВОВЫХ АКТОВ ==========
def fetch_legal_registry(start_date, end_date, in_omsk_oblast=None):
    if in_omsk_oblast is None:
        return {"status": "NO_DATA", "source": "Реестр правовых актов",
                "details": "Не удалось определить регион точки: омский реестр не применён"}
    if not in_omsk_oblast:
        return {"status": "NOT_APPLICABLE", "source": "Реестр правовых актов",
                "details": "Точка договора находится за пределами Омской области"}
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {"status": "NO_DATA", "source": "Реестр правовых актов",
                "details": "Неверный формат даты"}

    for ds, de, desc in LEGAL_ACTS:
        ds_dt = datetime.strptime(ds, "%Y-%m-%d")
        de_dt = datetime.strptime(de, "%Y-%m-%d")
        if sd <= de_dt and ed >= ds_dt:
            return {"status": "VERIFIED", "source": "Реестр правовых актов",
                    "details": f"{desc} (в силе с {ds} по {de})"}

    return {"status": "UNVERIFIED", "source": "Реестр правовых актов",
            "details": "Правовых актов о ЧС на указанный период нет"}


# ========== 3. NASA POWER ==========
async def fetch_nasa_power(client, lat, lon, start_date, end_date):
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y%m%d")
        ed = datetime.strptime(end_date, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return {"status": "NO_DATA", "source": "NASA POWER",
                "details": "Неверный формат даты"}

    try:
        r = await client.get(
            "https://power.larc.nasa.gov/api/temporal/daily/point",
            params={
                "parameters": "T2M_MAX,PRECTOTCORR",
                "community": "AG",
                "longitude": lon, "latitude": lat,
                "start": sd, "end": ed, "format": "JSON",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return {"status": "NO_DATA", "source": "NASA POWER",
                    "details": f"HTTP {r.status_code}"}

        data = r.json().get("properties", {}).get("parameter", {})
        temps = [t for t in data.get("T2M_MAX", {}).values()
                 if t is not None and t > -900]
        precips = [p for p in data.get("PRECTOTCORR", {}).values()
                   if p is not None and p >= 0]

        if not temps:
            return {"status": "NO_DATA", "source": "NASA POWER",
                    "details": "Нет данных"}

        max_temp = max(temps)
        total_precip = sum(precips)
        days = days_in_period(start_date, end_date)
        precip_limit = days * PRECIP_PER_DAY

        if max_temp > TEMP_MAX_THRESHOLD and total_precip < precip_limit:
            return {"status": "VERIFIED", "source": "NASA POWER",
                    "details": f"Засуха: макс. {max_temp:.1f}°C, {total_precip:.1f} мм "
                               f"(лимит {precip_limit:.0f})"}
        return {"status": "UNVERIFIED", "source": "NASA POWER",
                     "details": f"Макс. {max_temp:.1f}°C, осадки {total_precip:.1f} мм"}
    except Exception as e:
        return {"status": "NO_DATA", "source": "NASA POWER",
                "details": f"Ошибка: {str(e)[:60]}"}


# ========== 4. OPEN-METEO ==========
async def fetch_open_meteo(client, lat, lon, start_date, end_date):
    try:
        # Для страховой проверки используем только фактически опубликованные
        # архивные наблюдения. Forecast API намеренно не используется.
        requested_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        if requested_end >= date.today():
            return {"status": "NO_DATA", "source": "Open-Meteo Archive",
                    "details": "Период ещё не завершён: фактические данные не опубликованы"}
        endpoint = "https://archive-api.open-meteo.com/v1/archive"
        r = await client.get(
            endpoint,
            params={
                "latitude": lat, "longitude": lon,
                "start_date": start_date, "end_date": end_date,
                "daily": "temperature_2m_max,precipitation_sum",
                "timezone": "auto",
            },
            timeout=25,
        )
        if r.status_code != 200:
            return {"status": "NO_DATA", "source": "Open-Meteo Archive",
                    "details": f"HTTP {r.status_code}"}

        d = r.json().get("daily", {})
        temps = [t for t in d.get("temperature_2m_max", []) if t is not None]
        precips = [p for p in d.get("precipitation_sum", []) if p is not None]

        if not temps:
            return {"status": "NO_DATA", "source": "Open-Meteo Archive",
                    "details": "Нет данных"}

        max_temp = max(temps)
        total_precip = sum(precips)
        days = days_in_period(start_date, end_date)
        precip_limit = days * PRECIP_PER_DAY

        source_name = "Open-Meteo Archive"

        if max_temp > TEMP_MAX_THRESHOLD and total_precip < precip_limit:
            return {"status": "VERIFIED", "source": source_name,
                    "details": f"Засуха: макс. {max_temp:.1f}°C, {total_precip:.1f} мм "
                               f"(лимит {precip_limit:.0f})"}
        return {"status": "UNVERIFIED", "source": source_name,
                "details": f"Макс. {max_temp:.1f}°C, осадки {total_precip:.1f} мм"}
    except Exception as e:
        return {"status": "NO_DATA", "source": "Open-Meteo Archive",
                "details": f"Ошибка: {str(e)[:60]}"}


# ========== ЭНДПОИНТ ==========
@app.post("/api/check_contract")
async def check_contract(payload: ContractInput):
    try:
        start = datetime.strptime(payload.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(payload.end_date, "%Y-%m-%d").date()
    except ValueError:
        return {"detail": "Даты должны быть в формате YYYY-MM-DD"}
    if start > end:
        return {"detail": "Дата начала не может быть позже даты окончания"}
    if not (-90 <= payload.latitude <= 90 and -180 <= payload.longitude <= 180):
        return {"detail": "Некорректные координаты"}
    if payload.radius_km <= 0 or payload.insurance_sum < 0:
        return {"detail": "Радиус и страховая сумма должны быть неотрицательными"}
    bbox = build_bbox(payload.latitude, payload.longitude, payload.radius_km)

    async with httpx.AsyncClient() as client:
        nasa_task = fetch_nasa_power(client, payload.latitude, payload.longitude,
                                     payload.start_date, payload.end_date)
        om_task = fetch_open_meteo(client, payload.latitude, payload.longitude,
                                   payload.start_date, payload.end_date)
        nasa, om, in_omsk_oblast = await asyncio.gather(
            nasa_task, om_task,
            is_inside_omsk_oblast(client, payload.latitude, payload.longitude),
        )

    hist = fetch_historical_registry(payload.start_date, payload.end_date,
                                     in_omsk_oblast)
    legal = fetch_legal_registry(payload.start_date, payload.end_date,
                                 in_omsk_oblast)

    sources = [hist, legal, nasa, om]
    verified = [s for s in sources if s["status"] == "VERIFIED"]

    if len(verified) >= 3:
        payout = payload.insurance_sum * 0.30
        state = "PAID"
    elif len(verified) == 2:
        payout = payload.insurance_sum * 0.20
        state = "PARTIAL_PAID"
    elif len(verified) == 1:
        payout = payload.insurance_sum * 0.10
        state = "PARTIAL_PAID"
    else:
        payout = 0
        state = "NOT_ENOUGH_DATA"

    missing = []
    for s in sources:
        if s["status"] == "NO_DATA":
            missing.append(f"{s['source']}: {s['details']}")
        elif s["status"] == "UNVERIFIED":
            missing.append(f"{s['source']}: {s['details']} (не подтверждено)")

    return {
        "state": state, "payout": payout,
        "verified": verified, "missing_info": missing,
        "all_sources": sources, "bbox": bbox,
        "coordinates": {"lat": payload.latitude, "lon": payload.longitude,
                        "radius_km": payload.radius_km,
                        "in_omsk_oblast": in_omsk_oblast},
    }


app.mount("/static", StaticFiles(directory="."), name="static")


@app.get("/")
def index():
    return FileResponse("index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
