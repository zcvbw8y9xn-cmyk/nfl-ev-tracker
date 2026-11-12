"""Canonical NFL team, player, market, and sportsbook normalization."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

NL_TZ = ZoneInfo("America/St_Johns")

# User-selected Newfoundland-available betting sites (canonical names)
NL_BOOKS = [
    "ALC Proline",
    "Pinnacle",
    "bet365",
    "Sports Interaction",
    "BET99",
    "Betway",
]

# DFS Pick'em platforms tracked alongside NL sportsbooks
DFS_BOOKS = [
    "Sleeper",
    "PrizePicks",
    "Underdog",
]

# Sharp exchanges and market-making books prioritized for devigged fair value
SHARP_BENCHMARK_BOOKS = [
    "Pinnacle",
    "Novig",
    "ProphetX",
    "BookMaker.eu",
    "LowVig.ag",
    "BetOnline.ag",
    "Betfair Exchange",
    "SX Bet",
]

# Additional major US/CA regulated books & prediction markets used in consensus devigging
CONSENSUS_BOOKS = [
    *SHARP_BENCHMARK_BOOKS,
    "DraftKings",
    "FanDuel",
    "BetMGM",
    "Caesars",
    "BetRivers",
    "Hard Rock Bet",
    "Fanatics",
    "TonyBet",
    "theScore Bet",
    "Kalshi",
    "Polymarket",
    "bet365",
    "BET99",
    "Betway",
    "Sports Interaction",
]

TEAM_ABBR_MAP = {
    "ari": "ARI", "arizona": "ARI", "arizona cardinals": "ARI", "cardinals": "ARI",
    "atl": "ATL", "atlanta": "ATL", "atlanta falcons": "ATL", "falcons": "ATL",
    "bal": "BAL", "baltimore": "BAL", "baltimore ravens": "BAL", "ravens": "BAL",
    "buf": "BUF", "buffalo": "BUF", "buffalo bills": "BUF", "bills": "BUF",
    "car": "CAR", "carolina": "CAR", "carolina panthers": "CAR", "panthers": "CAR",
    "chi": "CHI", "chicago": "CHI", "chicago bears": "CHI", "bears": "CHI",
    "cin": "CIN", "cincinnati": "CIN", "cincinnati bengals": "CIN", "bengals": "CIN",
    "cle": "CLE", "cleveland": "CLE", "cleveland browns": "CLE", "browns": "CLE",
    "dal": "DAL", "dallas": "DAL", "dallas cowboys": "DAL", "cowboys": "DAL",
    "den": "DEN", "denver": "DEN", "denver broncos": "DEN", "broncos": "DEN",
    "det": "DET", "detroit": "DET", "detroit lions": "DET", "lions": "DET",
    "gb": "GB", "gnb": "GB", "green bay": "GB", "green bay packers": "GB", "packers": "GB",
    "hou": "HOU", "houston": "HOU", "houston texans": "HOU", "texans": "HOU",
    "ind": "IND", "indianapolis": "IND", "indianapolis colts": "IND", "colts": "IND",
    "jac": "JAX", "jax": "JAX", "jacksonville": "JAX", "jacksonville jaguars": "JAX", "jaguars": "JAX",
    "kc": "KC", "kan": "KC", "kansas city": "KC", "kansas city chiefs": "KC", "chiefs": "KC",
    "lv": "LV", "lvr": "LV", "oak": "LV", "las vegas": "LV", "las vegas raiders": "LV", "raiders": "LV",
    "lac": "LAC", "la chargers": "LAC", "los angeles c": "LAC", "los angeles chargers": "LAC", "chargers": "LAC",
    "los angelesc": "LAC", "losangelesc": "LAC", "la c": "LAC", "l.a. chargers": "LAC",
    "lar": "LA", "la": "LA", "la rams": "LA", "los angeles r": "LA", "los angeles rams": "LA", "rams": "LA",
    "los angelesr": "LA", "losangelesr": "LA", "la r": "LA", "l.a. rams": "LA",
    "mia": "MIA", "miami": "MIA", "miami dolphins": "MIA", "dolphins": "MIA",
    "min": "MIN", "minnesota": "MIN", "minnesota vikings": "MIN", "vikings": "MIN",
    "ne": "NE", "nwe": "NE", "new england": "NE", "new england patriots": "NE", "patriots": "NE",
    "no": "NO", "nor": "NO", "new orleans": "NO", "new orleans saints": "NO", "saints": "NO",
    "nyg": "NYG", "new york g": "NYG", "ny giants": "NYG", "new york giants": "NYG", "giants": "NYG",
    "nyj": "NYJ", "new york j": "NYJ", "ny jets": "NYJ", "new york jets": "NYJ", "jets": "NYJ",
    "phi": "PHI", "philadelphia": "PHI", "philadelphia eagles": "PHI", "eagles": "PHI",
    "pit": "PIT", "pittsburgh": "PIT", "pittsburgh steelers": "PIT", "steelers": "PIT",
    "sf": "SF", "sfo": "SF", "san francisco": "SF", "san francisco 49ers": "SF", "49ers": "SF", "niners": "SF", "9ers": "SF",
    "sanfrancisco": "SF", "sanfrancisco49ers": "SF", "san fran": "SF",
    "sea": "SEA", "seattle": "SEA", "seattle seahawks": "SEA", "seahawks": "SEA",
    "tb": "TB", "tam": "TB", "tampa bay": "TB", "tampa bay buccaneers": "TB", "buccaneers": "TB", "bucs": "TB",
    "ten": "TEN", "tennessee": "TEN", "tennessee titans": "TEN", "titans": "TEN",
    "was": "WAS", "wsh": "WAS", "washington": "WAS", "washington commanders": "WAS", "commanders": "WAS",
}


def normalize_team(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip().lower())
    s = s.replace(".", "")
    if not s:
        return ""
    if s in TEAM_ABBR_MAP:
        return TEAM_ABBR_MAP[s]
    # Longest-match on whole tokens only. Previously the code returned the *first*
    # insertion-ordered key found as a substring, which let e.g. "st louis rams" or
    # "san francisco giants" map to whichever key happened to appear earliest. Match
    # only keys that span full words, preferring the longest key.
    padded = f" {s} "
    for k in sorted(TEAM_ABBR_MAP, key=len, reverse=True):
        if len(k) <= 3:
            continue  # abbreviations only match exactly (handled above)
        if f" {k} " in padded:
            return TEAM_ABBR_MAP[k]
    # Fall back to the longest single-word city/nickname token that is an exact token.
    tokens = set(s.split())
    best = None
    for k, v in TEAM_ABBR_MAP.items():
        if " " in k or len(k) <= 3:
            continue
        if k in tokens and (best is None or len(k) > len(best[0])):
            best = (k, v)
    if best:
        return best[1]
    return (raw or "").strip().upper()


def normalize_player_name(name: str) -> str:
    """Normalize player names for cross-source matching (removes suffixes, punctuation, accents)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    # Remove Jr., Sr., II, III, IV
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_FIRST_NAME_ALIASES = {
    "cam": "cameron",
    "ken": "kenneth",
    "josh": "joshua",
    "zach": "zachary",
    "alex": "alexander",
    "gabe": "gabriel",
    "nick": "nicholas",
    "mitch": "mitchell",
    "ch": "chigoziem",
    "chig": "chigoziem",
    "pat": "patrick",
    "matt": "matthew",
    "jon": "jonathan",
    "ben": "benjamin",
    "dan": "daniel",
    "mike": "michael",
    "dave": "david",
    "rob": "robert",
    "tony": "anthony",
    "chris": "christopher",
    "deebo": "deebo",
    "hollywood": "marquise",
}


def player_match_key(name: str) -> str:
    """Build a collision-safe normalized key `'firstname_lastname'` (handles Jr./punctuation & common nicknames)."""
    norm = normalize_player_name(name)
    parts = norm.split()
    if len(parts) >= 2:
        first = _FIRST_NAME_ALIASES.get(parts[0], parts[0])
        return f"{first}_{parts[-1]}"
    return norm


MARKET_MAP = {
    # Passing
    "passing yards": "Passing Yards",
    "pass yards": "Passing Yards",
    "passing yds": "Passing Yards",
    "total passing yards": "Passing Yards",
    "player_pass_yds": "Passing Yards",
    "ftb-ttl passing yards": "Passing Yards",
    "passing touchdowns": "Passing TDs",
    "pass tds": "Passing TDs",
    "passing tds": "Passing TDs",
    "passing td": "Passing TDs",
    "touchdown passes": "Passing TDs",
    "td passes": "Passing TDs",
    "total touchdown passes": "Passing TDs",
    "player_pass_tds": "Passing TDs",
    "passing completions": "Pass Completions",
    "passing comps": "Pass Completions",
    "pass completions": "Pass Completions",
    "total pass completions": "Pass Completions",
    "player_pass_completions": "Pass Completions",
    "passing attempts": "Pass Attempts",
    "passing att": "Pass Attempts",
    "pass attempts": "Pass Attempts",
    "total pass attempts": "Pass Attempts",
    "player_pass_attempts": "Pass Attempts",
    "interceptions thrown": "Interceptions",
    "interceptions": "Interceptions",
    "ints thrown": "Interceptions",
    "passing ints": "Interceptions",
    "int": "Interceptions",
    "total interceptions": "Interceptions",
    "player_pass_interceptions": "Interceptions",
    "longest passing completion": "Longest Completion",
    "longest completion": "Longest Completion",
    "passing long": "Longest Completion",
    "player_pass_longest_completion": "Longest Completion",
    # Rushing
    "rushing yards": "Rushing Yards",
    "rush yards": "Rushing Yards",
    "rushing yds": "Rushing Yards",
    "total rushing yards": "Rushing Yards",
    "player_rush_yds": "Rushing Yards",
    "ftb-ttl rushing yards": "Rushing Yards",
    "rushing attempts": "Rush Attempts",
    "rushing att": "Rush Attempts",
    "rush attempts": "Rush Attempts",
    "rush att": "Rush Attempts",
    "rush try": "Rush Attempts",
    "total rush attempts": "Rush Attempts",
    "player_rush_attempts": "Rush Attempts",
    "longest rush": "Longest Rush",
    "rushing long": "Longest Rush",
    "player_rush_longest": "Longest Rush",
    # Receiving
    "receiving yards": "Receiving Yards",
    "rec yds": "Receiving Yards",
    "rec yards": "Receiving Yards",
    "receiving yds": "Receiving Yards",
    "recv yds": "Receiving Yards",
    "total receiving yards": "Receiving Yards",
    "player_reception_yds": "Receiving Yards",
    "ftb-ttl receiving yards": "Receiving Yards",
    "receptions": "Receptions",
    "recs": "Receptions",
    "rec": "Receptions",
    "reception": "Receptions",
    "receptions made": "Receptions",
    "receiving rec": "Receptions",
    "total receptions": "Receptions",
    "player_receptions": "Receptions",
    "ftb - ttl receptions": "Receptions",
    "longest reception": "Longest Reception",
    "receiving long": "Longest Reception",
    "player_reception_longest": "Longest Reception",
    "targets": "Targets",
    "rec targets": "Targets",
    "receiving tgts": "Targets",
    # Combo & TDs
    "rushing + receiving yards": "Rush + Rec Yards",
    "rushing receiving yards": "Rush + Rec Yards",
    "rush + rec yards": "Rush + Rec Yards",
    "rush+rec yds": "Rush + Rec Yards",
    "rush rec yds": "Rush + Rec Yards",
    "player_rush_reception_yds": "Rush + Rec Yards",
    "passing + rushing yards": "Pass + Rush Yards",
    "pass + rush yards": "Pass + Rush Yards",
    "pass + rush yds": "Pass + Rush Yards",
    "passing and rushing yds": "Pass + Rush Yards",
    "pass+rush yds": "Pass + Rush Yards",
    "anytime touchdown": "Anytime TD",
    "anytime touchdowns": "Anytime TD",
    "anytime tds": "Anytime TD",
    "anytime td": "Anytime TD",
    "anytime touchdown scorer": "Anytime TD",
    "rushing + receiving touchdowns": "Anytime TD",
    "rush rec tds": "Anytime TD",
    "total touchdowns": "Anytime TD",
    "player_anytime_td": "Anytime TD",
    "player_tds_over": "Anytime TD",
    "ftb - touchdown scorers": "Anytime TD",
    "passing + rushing touchdowns": "Pass + Rush TDs",
    "passing + rushing tds": "Pass + Rush TDs",
    "pass + rush touchdowns": "Pass + Rush TDs",
    "pass + rush tds": "Pass + Rush TDs",
    "rushing touchdowns": "Rushing TDs",
    "rushing tds": "Rushing TDs",
    "rushing td": "Rushing TDs",
    "rush touchdowns": "Rushing TDs",
    "rush tds": "Rushing TDs",
    "player_rush_tds": "Rushing TDs",
    "receiving touchdowns": "Receiving TDs",
    "receiving tds": "Receiving TDs",
    "receiving td": "Receiving TDs",
    "rec touchdowns": "Receiving TDs",
    "rec tds": "Receiving TDs",
    "player_rec_tds": "Receiving TDs",
    "1st touchdown scorer": "1st TD",
    "first touchdown": "1st TD",
    "first td scorer": "1st TD",
    "player_1st_td": "1st TD",
    "ftb - 1st touchdown scorer": "1st TD",
    "last touchdown scorer": "Last TD",
    "last_touchdown_scored": "Last TD",
    "last touchdown": "Last TD",
    "last td scorer": "Last TD",
    "last td": "Last TD",
    "player_last_td": "Last TD",
    "kicking points": "Kicking Points",
    "player_kicking_points": "Kicking Points",
    "field goals made": "FG Made",
    "fg made": "FG Made",
    "total field goals": "FG Made",
    "total fg": "FG Made",
    "player_field_goals": "FG Made",
    "player_sacks": "Sacks",
    "sacks": "Sacks",
    "player_tackles_assists": "Tackles + Assists",
    "tackles + assists": "Tackles + Assists",
    "tackles assists": "Tackles + Assists",
    "player_assists": "Assists",
    "assists": "Assists",
    "fantasy score": "Fantasy Points",
    "fantasy points": "Fantasy Points",
    "fantasy score ppr": "Fantasy Points (PPR)",
    "fantasy points ppr": "Fantasy Points (PPR)",
    "fantasy points (ppr)": "Fantasy Points (PPR)",
    "fantasy score (ppr)": "Fantasy Points (PPR)",
    "ppr fantasy points": "Fantasy Points (PPR)",
    "ppr fantasy score": "Fantasy Points (PPR)",
    "fantasy points full ppr": "Fantasy Points (PPR)",
    "fantasy score full ppr": "Fantasy Points (PPR)",
    "fantasy score half ppr": "Fantasy Points (Half PPR)",
    "fantasy points half ppr": "Fantasy Points (Half PPR)",
    "fantasy points (half ppr)": "Fantasy Points (Half PPR)",
    "fantasy score (half ppr)": "Fantasy Points (Half PPR)",
    "half ppr fantasy points": "Fantasy Points (Half PPR)",
    "half ppr fantasy score": "Fantasy Points (Half PPR)",
    "fantasy points 0.5 ppr": "Fantasy Points (Half PPR)",
    "fantasy score 0.5 ppr": "Fantasy Points (Half PPR)",
    "fantasy score standard": "Fantasy Points (Standard)",
    "fantasy points standard": "Fantasy Points (Standard)",
    "fantasy points (standard)": "Fantasy Points (Standard)",
    "fantasy score (standard)": "Fantasy Points (Standard)",
    "standard fantasy points": "Fantasy Points (Standard)",
    "standard fantasy score": "Fantasy Points (Standard)",
    "fantasy points 0 ppr": "Fantasy Points (Standard)",
    "fantasy points 0ppr": "Fantasy Points (Standard)",
    "fantasy points non ppr": "Fantasy Points (Standard)",
    "fantasy points no ppr": "Fantasy Points (Standard)",
    "fantasy points (alc)": "Fantasy Points (ALC)",
    "fantasy points alc": "Fantasy Points (ALC)",
}


_DERIVED_NOISE_WORDS = (
    "per carry", "first 5", "first 10", "consecutive", "in first",
    "quarter", "1q", "1h", "2h", "2q", "3q", "4q", "combo",
    "2+", "3+", "4+", "2nd", "3rd", "multiple", "team",
    "defense", "defensive", "d/st", "dst", "special teams", "return", "longest", "next",
)
_MARKET_MAP_BY_LEN = sorted(MARKET_MAP.items(), key=lambda kv: -len(kv[0]))


def _normalize_td_market(s: str, raw: str) -> str:
    """Strictly separate TD markets across QB, RB, WR, TE, K, and DEF so no variant collapses into Anytime TD."""
    tokens = set(re.findall(r"[a-z0-9+]+", s))
    if any(w in s for w in _DERIVED_NOISE_WORDS) or re.search(r"\bhalf\b(?!\s*ppr)", s):
        return (raw or "").strip().title()
    has_pass = bool({"pass", "passing", "passes"} & tokens)
    has_rush = bool({"rush", "rushing"} & tokens)
    has_rec = bool({"rec", "receiving", "reception", "receptions"} & tokens)
    has_anytime = "anytime" in tokens or "ftb - touchdown scorers" in s
    if has_pass and has_rush and has_rec:
        return "Pass + Rush + Rec TDs"
    if has_pass and has_rush:
        return "Pass + Rush TDs"
    if has_pass and has_rec:
        return "Pass + Rec TDs"
    if has_pass:
        return "Passing TDs"
    if has_rush and has_rec:
        return "Anytime TD"
    if has_rush:
        return "Rushing TDs"
    if has_rec:
        return "Receiving TDs"
    if "1st" in tokens or "first" in tokens:
        return "1st TD"
    if "last" in tokens:
        return "Last TD"
    if has_anytime or s in ("touchdowns", "total touchdowns", "td", "tds", "score a touchdown", "to score a touchdown"):
        return "Anytime TD"
    return (raw or "").strip().title()


def normalize_market(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip().lower()).replace("_", " ").replace("-", " ")
    # direct lookup
    s_raw_lower = (raw or "").strip().lower()
    if s_raw_lower in MARKET_MAP:
        return MARKET_MAP[s_raw_lower]
    if s in MARKET_MAP:
        return MARKET_MAP[s]
    # Decipher Half PPR / Standard / Full PPR even if combined with custom wording
    if "fant" in s and "ppr" in s:
        if "half" in s or "0.5" in s or ".5" in s:
            return "Fantasy Points (Half PPR)"
        if "0 ppr" in s or "0ppr" in s or "non ppr" in s or "no ppr" in s:
            return "Fantasy Points (Standard)"
        return "Fantasy Points (PPR)"
    if "fant" in s and ("standard" in s or "std" in s.split()):
        return "Fantasy Points (Standard)"
    # Strictly handle ALL touchdown markets before generic substring matching
    if re.search(r"\b(?:tds?|touchdowns?)\b", s):
        return _normalize_td_market(s, raw)
    # Do not let substring matching collapse derived/partial markets (e.g. 'Rush Yards Per Carry', '1st Half Yards')
    if any(w in s for w in _DERIVED_NOISE_WORDS) or re.search(r"\bhalf\b(?!\s*ppr)", s):
        return (raw or "").strip().title()
    # substring matching (longest key first so 'passing + rushing yards' beats 'rushing yards')
    for k, v in _MARKET_MAP_BY_LEN:
        if k in s_raw_lower or k in s:
            return v
    return (raw or "").strip().title()


def normalize_book_name(raw: str) -> str:
    s = (raw or "").strip().lower().replace(" logo", "")
    if "alc" in s or "proline" in s or "stadium" in s:
        return "ALC Proline"
    if "pinnacle" in s:
        return "Pinnacle"
    if "365" in s:
        return "bet365"
    if "interaction" in s or s == "sia":
        return "Sports Interaction"
    if "bet99" in s:
        return "BET99"
    if "betway" in s:
        return "Betway"
    if "coolbet" in s:
        return "Coolbet"
    if "tonybet" in s:
        return "TonyBet"
    if "novig" in s:
        return "Novig"
    if "prophetx" in s:
        return "ProphetX"
    if "bookmaker" in s:
        return "BookMaker.eu"
    if "lowvig" in s:
        return "LowVig.ag"
    if "betonline" in s:
        return "BetOnline.ag"
    if "betfair" in s:
        return "Betfair Exchange"
    if "sx bet" in s or s == "sxbet":
        return "SX Bet"
    if "polymarket" in s:
        return "Polymarket"
    if "kalshi" in s:
        return "Kalshi"
    if "pick6" in s:
        return "DraftKings Pick6"
    if "draftkings" in s:
        return "DraftKings"
    if "fanduel" in s:
        return "FanDuel"
    if "betmgm" in s:
        return "BetMGM"
    if "caesars" in s or "william hill" in s:
        return "Caesars"
    if "betrivers" in s:
        return "BetRivers"
    if "hard rock" in s:
        return "Hard Rock Bet"
    if "fanatics" in s:
        return "Fanatics"
    if "score" in s:
        return "theScore Bet"
    if "bovada" in s:
        return "Bovada"
    if "sleeper" in s:
        return "Sleeper"
    if "prizepicks" in s:
        return "PrizePicks"
    if "underdog" in s:
        return "Underdog"
    return (raw or "").strip()


def to_nl_time_str(iso_or_dt: str | datetime) -> str:
    """Format an ISO timestamp or datetime into Newfoundland Time (e.g., 'Thu Nov 12 9:45 PM NST')."""
    if isinstance(iso_or_dt, str):
        dt = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
    else:
        dt = iso_or_dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    nl = dt.astimezone(NL_TZ)
    return nl.strftime("%a %b %-d %-I:%M %p NST")
