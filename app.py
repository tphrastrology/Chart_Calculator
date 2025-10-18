# Chart_Calculator — Free and simple astrological natal chart API
# Copyright (C) 2025  TPHR Astrology
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from datetime import datetime
from dateutil import tz
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import Optional

# flatlib / Swiss Ephemeris
from flatlib import const, aspects, angle
from flatlib.geopos import GeoPos
from flatlib.datetime import Datetime
from flatlib.chart import Chart
import flatlib.ephem
flatlib.ephem.ephepath = "."  # or "ephe" if you move the files later



app = FastAPI(title="Natal Chart API", version="1.0.0")

HOUSE_MAP = {
    "Placidus": const.HOUSES_PLACIDUS,
    "Koch": const.HOUSES_KOCH,
    "Porphyry": const.HOUSES_PORPHYRIUS,       # <- correct constant name
    "Porphyrius": const.HOUSES_PORPHYRIUS,     # allow either spelling
    "Regiomontanus": const.HOUSES_REGIOMONTANUS,
    "Campanus": const.HOUSES_CAMPANUS,
    "Equal": const.HOUSES_EQUAL,
    "WholeSign": const.HOUSES_WHOLESIGN
}



PLANET_LIST = [
    const.SUN, const.MOON, const.MERCURY, const.VENUS, const.MARS,
    const.JUPITER, const.SATURN, const.URANUS, const.NEPTUNE, const.PLUTO,
    const.NORTH_NODE, const.SOUTH_NODE
]

ASPECTS = [
    (aspects.CONJUNCTION, 8),
    (aspects.OPPOSITION, 8),
    (aspects.TRINE, 7),
    (aspects.SQUARE, 6),
    (aspects.SEXTILE, 5),
]

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def lon_to_sign_deg(lon):
    lon = lon % 360.0
    sign_index = int(lon // 30)
    deg_in_sign = lon - sign_index * 30
    return SIGNS[sign_index], round(deg_in_sign, 2)

def angle_to_obj(name, lon):
    sign, deg = lon_to_sign_deg(lon)
    return {"name": name, "sign": sign, "deg": deg, "lon": round(lon % 360.0, 4)}

class NatalInput(BaseModel):
    date: str = Field(..., example="1990-06-12")          # YYYY-MM-DD
    time: str = Field(..., example="14:23")               # 24h HH:MM
    timezone: str = Field(..., example="America/New_York")# IANA timezone
    latitude: float
    longitude: float
    house_system: Optional[str] = Field(default="Placidus")

    @field_validator("house_system")
    @classmethod
    def valid_house(cls, v):
        if v not in HOUSE_MAP:
            raise ValueError(f"house_system must be one of: {', '.join(HOUSE_MAP.keys())}")
        return v

ALIASES = {
    "porphyry": "Porphyry",
    "porphyrius": "Porphyrius",
    "wholesign": "WholeSign",
    "whole sign": "WholeSign",
}

@field_validator("house_system")
@classmethod
def valid_house(cls, v):
    key = ALIASES.get(str(v).strip().lower(), v)
    if key not in HOUSE_MAP:
        raise ValueError(f"house_system must be one of: {', '.join(HOUSE_MAP.keys())}")
    return key

def to_utc_iso(date_str, time_str, tzname):
    local_tz = tz.gettz(tzname)
    if not local_tz:
        raise ValueError("Invalid timezone string. Use IANA, e.g., 'America/New_York'.")
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    local_dt = naive.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(tz.UTC)
    return utc_dt, utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

@app.post("/natal")
def natal(payload: NatalInput):
    try:
        utc_dt, utc_iso = to_utc_iso(payload.date, payload.time, payload.timezone)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    fl_dt = Datetime(utc_dt.strftime("%Y/%m/%d"), utc_dt.strftime("%H:%M"), "+00:00")
    pos = GeoPos(lat=payload.latitude, lon=payload.longitude)
    chart = Chart(fl_dt, pos, IDs=PLANET_LIST, hsys=HOUSE_MAP[payload.house_system])

    # angles & houses
    asc = angle.ASC(chart); mc = angle.MC(chart)
    houses = []
    for i in range(1, 13):
        cusp = chart.houses.getHouse(i)
        s, d = lon_to_sign_deg(cusp.lon)
        houses.append({"n": i, "sign": s, "deg": d, "lon": round(cusp.lon % 360.0, 4)})

    # planets
    def pt(name):
        p = chart.get(name)
        s, d = lon_to_sign_deg(p.lon)
        return {"name": p.body, "sign": s, "deg": d, "lon": round(p.lon % 360.0, 4),
                "lat": round(getattr(p, "lat", 0.0), 4), "speed": round(getattr(p, "speed", 0.0), 5)}
    planets = [pt(p) for p in PLANET_LIST]

    # aspects
    asp_results = []
    for i, a in enumerate(PLANET_LIST):
        A = chart.get(a); lonA = A.lon % 360.0
        for b in PLANET_LIST[i+1:]:
            B = chart.get(b); lonB = B.lon % 360.0
            dist = min(abs(lonA - lonB), 360.0 - abs(lonA - lonB))
            for asp_type, orb in ASPECTS:
                exact = aspects.ASPECTS[asp_type]
                diff = abs(dist - exact)
                if diff <= orb:
                    asp_results.append({
                        "a": A.body, "b": B.body,
                        "type": aspects.NAME[asp_type],
                        "orb": round(diff, 2), "dist": round(dist, 2), "exact": exact
                    })
                    break

    asc_obj = angle_to_obj("ASC", asc.lon)
    mc_obj  = angle_to_obj("MC",  mc.lon)

    return {
        "meta": {"house_system": payload.house_system, "datetime_utc": utc_iso,
                 "location": {"lat": payload.latitude, "lng": payload.longitude}},
        "angles": {"ASC": asc_obj, "MC": mc_obj},
        "houses": houses,
        "planets": planets,
        "aspects": asp_results,
        "rising_sign": {"sign": asc_obj["sign"], "deg": asc_obj["deg"]}
    }

@app.get("/healthz")
def health():
    return {"ok": True}
