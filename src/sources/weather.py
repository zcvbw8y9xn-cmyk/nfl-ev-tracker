"""Multi-source NFL stadium field weather aggregator (Open-Meteo + NOAA NWS + RotoWire + ESPN)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
from zoneinfo import ZoneInfo

from .cache_utils import fetch_with_cache, pace_domain

NL_TZ = ZoneInfo("America/St_Johns")
NWS_GRID_CACHE_PATH = Path.home() / ".cache" / "nfl-ev-tracker" / "nws_grid_cache.json"

# Canonical NFL home stadium coordinates (lat, lon, is_dome_by_default)
STADIUM_COORDS_BY_TEAM: Dict[str, Tuple[float, float, bool]] = {
    "ARI": (33.5276, -112.2626, True),   # State Farm Stadium (retractable)
    "ATL": (33.7554, -84.4009, True),    # Mercedes-Benz Stadium (retractable)
    "BAL": (39.2780, -76.6227, False),   # M&T Bank Stadium
    "BUF": (42.7738, -78.7870, False),   # Highmark Stadium
    "CAR": (35.2258, -80.8528, False),   # Bank of America Stadium
    "CHI": (41.8623, -87.6167, False),   # Soldier Field
    "CIN": (39.0955, -84.5161, False),   # Paycor Stadium
    "CLE": (41.5061, -81.6995, False),   # Huntington Bank Field
    "DAL": (32.7473, -97.0945, True),    # AT&T Stadium (retractable)
    "DEN": (39.7439, -105.0201, False),  # Empower Field at Mile High
    "DET": (42.3400, -83.0456, True),    # Ford Field
    "GB":  (44.5013, -88.0622, False),   # Lambeau Field
    "HOU": (29.6847, -95.4107, True),    # NRG Stadium (retractable)
    "IND": (39.7601, -86.1639, True),    # Lucas Oil Stadium (retractable)
    "JAX": (30.3239, -81.6373, False),   # EverBank Stadium
    "KC":  (39.0489, -94.4839, False),   # GEHA Field at Arrowhead
    "LV":  (36.0909, -115.1833, True),   # Allegiant Stadium
    "LAC": (33.9535, -118.3392, True),   # SoFi Stadium
    "LAR": (33.9535, -118.3392, True),   # SoFi Stadium
    "MIA": (25.9580, -80.2389, False),   # Hard Rock Stadium
    "MIN": (44.9736, -93.2575, True),    # U.S. Bank Stadium
    "NE":  (42.0909, -71.2643, False),   # Gillette Stadium
    "NO":  (29.9511, -90.0812, True),    # Caesars Superdome
    "NYG": (40.8135, -74.0744, False),   # MetLife Stadium
    "NYJ": (40.8135, -74.0744, False),   # MetLife Stadium
    "PHI": (39.9008, -75.1675, False),   # Lincoln Financial Field
    "PIT": (40.4468, -80.0158, False),   # Acrisure Stadium
    "SEA": (47.5952, -122.3316, False),  # Lumen Field
    "SF":  (37.4033, -121.9694, False),  # Levi's Stadium
    "TB":  (27.9759, -82.5033, False),   # Raymond James Stadium
    "TEN": (36.1665, -86.7713, False),   # Nissan Stadium
    "WAS": (38.9076, -76.8645, False),   # Northwest Stadium
}

# International / neutral-site NFL venues matched by venue substring
INTERNATIONAL_VENUE_COORDS: List[Tuple[str, Tuple[float, float, bool]]] = [
    ("tottenham", (51.6042, -0.0662, False)),
    ("wembley", (51.5560, -0.2796, False)),
    ("allianz", (48.2188, 11.6247, False)),
    ("munich", (48.2188, 11.6247, False)),
    ("bayern", (48.2188, 11.6247, False)),
    ("olympiastadion", (52.5147, 13.2395, False)),
    ("deutsche bank park", (50.0686, 8.6455, False)),
    ("frankfurt", (50.0686, 8.6455, False)),
    ("bernabeu", (40.4530, -3.6883, True)),
    ("arena corinthians", (-23.5453, -46.4742, False)),
    ("azteca", (19.3029, -99.1505, False)),
    ("melbourne", (-37.8200, 144.9834, False)),
]

WMO_WEATHER_CODES: Dict[int, str] = {
    0: "Clear",
    1: "Mostly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    80: "Rain Showers",
    81: "Rain Showers",
    82: "Heavy Showers",
    85: "Snow Showers",
    86: "Heavy Snow",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


def _resolve_stadium_coords(game: Dict[str, Any]) -> Optional[Tuple[float, float, bool]]:
    venue_low = (game.get("venue") or "").lower()
    for substr, coords in INTERNATIONAL_VENUE_COORDS:
        if substr in venue_low:
            return coords
    home = game.get("home", "")
    return STADIUM_COORDS_BY_TEAM.get(home)


def _parse_kickoff_utc(iso_str: str) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _closest_hour_idx(times_iso: List[str], target_utc: datetime) -> Optional[int]:
    if not times_iso:
        return None
    best_i = None
    best_diff = 10**12
    for i, tstr in enumerate(times_iso):
        dt = _parse_kickoff_utc(tstr if ("Z" in tstr or "+" in tstr) else tstr + "Z")
        if dt is None:
            continue
        diff = abs((dt - target_utc).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_i = i
    return best_i if best_diff <= 6 * 3600 else None


def _fetch_open_meteo_batch(targets: List[Tuple[str, float, float, datetime]]) -> Dict[str, Dict[str, Any]]:
    """Fetch kickoff-hour stadium weather for all target games in a single Open-Meteo call."""
    if not targets:
        return {}
    lats = ",".join(f"{lat:.4f}" for _, lat, _, _ in targets)
    lons = ",".join(f"{lon:.4f}" for _, _, lon, _ in targets)
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lats}&longitude={lons}"
        f"&hourly=temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m,weather_code"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=UTC&forecast_days=10"
    )
    out: Dict[str, Dict[str, Any]] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NFLEVTracker/1.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
        entries = raw if isinstance(raw, list) else [raw]
        for (matchup, _, _, ko_utc), entry in zip(targets, entries):
            hourly = entry.get("hourly") or {}
            idx = _closest_hour_idx(hourly.get("time") or [], ko_utc)
            if idx is None:
                continue
            precip_arr = hourly.get("precipitation") or []
            pop_arr = hourly.get("precipitation_probability") or []
            temp = (hourly.get("temperature_2m") or [None])[idx]
            pop = pop_arr[idx] if idx < len(pop_arr) else None
            precip = precip_arr[idx] if idx < len(precip_arr) else None
            # Sum predicted precipitation (mm) over the ~3h game window following kickoff
            win_end = min(len(precip_arr), idx + 4)
            win_start = min(idx + 1, win_end)
            game_slice = [float(x) for x in precip_arr[win_start:win_end] if x is not None]
            if not game_slice and precip is not None:
                game_slice = [float(precip)]
            game_precip_mm = round(sum(game_slice), 1)
            win_pop_slice = [float(x) for x in pop_arr[idx:min(len(pop_arr), idx + 4)] if x is not None]
            game_pop_max = round(max(win_pop_slice)) if win_pop_slice else (round(float(pop)) if pop is not None else None)
            wind = (hourly.get("wind_speed_10m") or [None])[idx]
            gust = (hourly.get("wind_gusts_10m") or [None])[idx]
            code = (hourly.get("weather_code") or [None])[idx]
            cond = WMO_WEATHER_CODES.get(int(code), "Clear") if code is not None else "Clear"
            out[matchup] = {
                "temp_f": round(float(temp)) if temp is not None else None,
                "precip_pct": round(float(pop)) if pop is not None else None,
                "game_precip_pct_max": game_pop_max,
                "precip_mm": round(float(precip), 1) if precip is not None else 0.0,
                "game_precip_mm": game_precip_mm,
                "wind_mph": round(float(wind)) if wind is not None else None,
                "gust_mph": round(float(gust)) if gust is not None else None,
                "cond": cond,
            }
    except Exception:
        return {}
    return out


def _load_nws_grid_cache() -> Dict[str, str]:
    if NWS_GRID_CACHE_PATH.exists():
        try:
            data = json.loads(NWS_GRID_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_nws_grid_cache(cache: Dict[str, str]) -> None:
    try:
        NWS_GRID_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = NWS_GRID_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(NWS_GRID_CACHE_PATH)
    except Exception:
        pass


def _parse_nws_wind_mph(wind_str: str) -> Optional[int]:
    nums = [int(x) for x in re.findall(r"\d+", wind_str or "")]
    if not nums:
        return None
    return round(sum(nums) / len(nums))


def _fetch_nws_single(matchup: str, lat: float, lon: float, ko_utc: datetime, grid_cache: Dict[str, str]) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    # NWS only covers US territories (~lat 18..72, lon -179..-64)
    if not (18.0 <= lat <= 72.0 and -179.0 <= lon <= -64.0):
        return matchup, None, None
    coord_key = f"{lat:.4f},{lon:.4f}"
    fc_url = grid_cache.get(coord_key)
    headers = {"User-Agent": "NFLEVTracker/1.0 (local-app)", "Accept": "application/geo+json"}
    try:
        if not fc_url:
            pace_domain("api.weather.gov", min_delay=0.25, jitter=0.2)
            pts_req = urllib.request.Request(f"https://api.weather.gov/points/{coord_key}", headers=headers)
            pts = json.loads(urllib.request.urlopen(pts_req, timeout=10).read().decode("utf-8"))
            fc_url = (pts.get("properties") or {}).get("forecastHourly")
            if not fc_url:
                return matchup, None, None

        pace_domain("api.weather.gov", min_delay=0.25, jitter=0.2)
        fc_req = urllib.request.Request(fc_url, headers=headers)
        fc = json.loads(urllib.request.urlopen(fc_req, timeout=10).read().decode("utf-8"))
        periods = (fc.get("properties") or {}).get("periods") or []
        best_p = None
        best_diff = 10**12
        for p in periods:
            dt = _parse_kickoff_utc(p.get("startTime", ""))
            if dt is None:
                continue
            diff = abs((dt - ko_utc).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_p = p
        if not best_p or best_diff > 4 * 3600:
            return matchup, None, fc_url
        pop_val = (best_p.get("probabilityOfPrecipitation") or {}).get("value")
        return (
            matchup,
            {
                "temp_f": round(float(best_p["temperature"])) if best_p.get("temperature") is not None else None,
                "wind_mph": _parse_nws_wind_mph(best_p.get("windSpeed", "")),
                "precip_pct": round(float(pop_val)) if pop_val is not None else None,
                "cond": (best_p.get("shortForecast") or "").strip(),
            },
            fc_url,
        )
    except Exception:
        return matchup, None, None


def enrich_games_with_weather(espn_games: List[Dict[str, Any]], rw_weather_map: Dict[str, str]) -> None:
    """Populate live, multi-source kickoff field weather onto each ESPN game dict in place."""
    now_utc = datetime.now(timezone.utc)
    updated_display = datetime.now(NL_TZ).strftime("%I:%M %p NST")
    targets: List[Tuple[str, float, float, datetime]] = []

    for g in espn_games:
        coords = _resolve_stadium_coords(g)
        ko = _parse_kickoff_utc(g.get("kickoff_iso", "")) or now_utc
        if coords:
            targets.append((g["matchup"], coords[0], coords[1], ko))

    slate_tag = hashlib.sha256("|".join(sorted(m for m, _, _, _ in targets)).encode("utf-8")).hexdigest()[:12]
    om_map = fetch_with_cache(
        f"open_meteo_v2_{slate_tag}",
        "api.open-meteo.com",
        ttl_seconds=1200.0,
        fetcher=lambda: _fetch_open_meteo_batch(targets),
        is_valid=lambda d: isinstance(d, dict) and len(d) > 0 and any("game_precip_mm" in v for v in d.values()),
        min_delay=0.25,
        jitter=0.15,
    ) or {}

    def _fetch_all_nws() -> Dict[str, Dict[str, Any]]:
        grid_cache = _load_nws_grid_cache()
        out_nws: Dict[str, Dict[str, Any]] = {}
        cache_dirty = False
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = [pool.submit(_fetch_nws_single, m, lat, lon, ko, grid_cache) for m, lat, lon, ko in targets]
            for fut in as_completed(futs):
                m, res, fc_url = fut.result()
                if fc_url and fc_url not in grid_cache.values():
                    for tm, lat, lon, _ in targets:
                        if tm == m:
                            grid_cache[f"{lat:.4f},{lon:.4f}"] = fc_url
                            cache_dirty = True
                            break
                if res:
                    out_nws[m] = res
        if cache_dirty:
            _save_nws_grid_cache(grid_cache)
        return out_nws

    nws_map = fetch_with_cache(
        f"nws_weather_{slate_tag}",
        "api.weather.gov",
        ttl_seconds=1200.0,
        fetcher=_fetch_all_nws,
        is_valid=lambda d: isinstance(d, dict) and len(d) > 0,
        min_delay=0.2,
        jitter=0.1,
    ) or {}

    for g in espn_games:
        m = g["matchup"]
        om = om_map.get(m)
        nw = nws_map.get(m)
        rw = (rw_weather_map.get(m) or "").strip()
        es = (g.get("espn_weather") or "").strip()

        sources_detail: Dict[str, str] = {}
        game_precip_mm = om.get("game_precip_mm") if om else None
        if om:
            om_str = f"{om['cond']}, {om['temp_f']}F, Wind {om['wind_mph']}mph"
            if om.get("gust_mph"):
                om_str += f" (G{om['gust_mph']})"
            if om.get("precip_pct") is not None:
                om_str += f", {om['precip_pct']}% precip"
            if game_precip_mm is not None:
                om_str += f", {game_precip_mm:.1f}mm/3h"
            sources_detail["Open-Meteo"] = om_str
        if nw:
            nw_parts = [p for p in [nw.get("cond"), f"{nw['temp_f']}F" if nw.get("temp_f") is not None else None, f"Wind {nw['wind_mph']}mph" if nw.get("wind_mph") is not None else None, f"{nw['precip_pct']}% precip" if nw.get("precip_pct") is not None else None] if p]
            sources_detail["NOAA NWS"] = ", ".join(nw_parts)
        if rw:
            sources_detail["RotoWire"] = rw
        if es:
            sources_detail["ESPN"] = es

        is_indoor = bool(g.get("indoor"))
        temps = [x["temp_f"] for x in (om, nw) if x and x.get("temp_f") is not None]
        winds = [x["wind_mph"] for x in (om, nw) if x and x.get("wind_mph") is not None]
        pops = [x["precip_pct"] for x in (om, nw) if x and x.get("precip_pct") is not None]

        avg_temp = round(sum(temps) / len(temps)) if temps else None
        avg_wind = round(sum(winds) / len(winds)) if winds else None
        max_wind = max(winds) if winds else None
        avg_pop = round(sum(pops) / len(pops)) if pops else None
        gust = om.get("gust_mph") if om else None
        cond = (om or {}).get("cond") or (nw or {}).get("cond") or ""

        # Determine fantasy/betting weather impact badge
        cond_low = f"{cond} {rw} {es}".lower()
        if is_indoor:
            impact = "Dome"
        elif "snow" in cond_low:
            impact = "Snow"
        elif "rain" in cond_low or "shower" in cond_low or "drizzle" in cond_low or (avg_pop is not None and avg_pop >= 50) or (game_precip_mm is not None and game_precip_mm >= 0.5):
            impact = "Rain / Wet"
        elif (avg_wind is not None and avg_wind >= 15) or (gust is not None and gust >= 24):
            impact = "High Wind"
        elif avg_temp is not None and avg_temp <= 34:
            impact = "Freezing"
        else:
            impact = "Clear"

        if is_indoor:
            outside_hint = f" (Outside: {avg_temp}F, {cond})" if avg_temp is not None and cond else ""
            summary = f"Dome / Indoor{outside_hint}"
        elif temps or winds:
            pieces = []
            if cond:
                pieces.append(cond)
            if avg_temp is not None:
                pieces.append(f"{avg_temp}F")
            if avg_wind is not None:
                w_txt = f"Wind {avg_wind}mph"
                if gust and gust > avg_wind + 3:
                    w_txt += f" (G{gust})"
                pieces.append(w_txt)
            if avg_pop is not None and avg_pop >= 15 and game_precip_mm is not None:
                pieces.append(f"{avg_pop}% precip ({game_precip_mm:.1f}mm game)")
            elif avg_pop is not None and avg_pop >= 15:
                pieces.append(f"{avg_pop}% precip")
            elif game_precip_mm is not None:
                pieces.append(f"{game_precip_mm:.1f}mm precip (game)")
            summary = ", ".join(pieces)
        elif rw or es:
            summary = rw or es
        else:
            summary = "Forecast unavailable"

        g["weather"] = summary
        g["weather_impact"] = impact
        g["weather_updated_at"] = updated_display
        g["weather_sources"] = sources_detail
        g["weather_metrics"] = {
            "temp_f": avg_temp,
            "wind_mph": avg_wind,
            "max_wind_mph": max_wind,
            "gust_mph": gust,
            "precip_pct": avg_pop,
            "game_precip_mm": game_precip_mm,
        }
