"""BOAT RACE 公式サイトスクレイパー。

対象ページ:
  - 出走表     /owpc/pc/race/racelist
  - 直前情報   /owpc/pc/race/beforeinfo  (気象データも含む)
  - オッズ     /owpc/pc/race/odds3t (3連単), odds3f (3連複), odds2tf (2連単/複)
  - レース結果 /owpc/pc/race/raceresult  (払戻データも含む)
"""
import re
import warnings
from datetime import date
from typing import Optional
import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .base import BaseScraper
from src.utils.helpers import safe_float, safe_int
from src.utils.logger import get_logger

logger = get_logger(__name__)

_URLS = {
    "racelist":   "/owpc/pc/race/racelist",
    "beforeinfo": "/owpc/pc/race/beforeinfo",
    "odds3t":     "/owpc/pc/race/odds3t",
    "odds3f":     "/owpc/pc/race/odds3f",
    "odds2tf":    "/owpc/pc/race/odds2tf",
    "oddstf":     "/owpc/pc/race/oddstf",     # 単勝・複勝
    "raceresult": "/owpc/pc/race/raceresult",
    "index":      "/owpc/pc/race/index",
    "pay":        "/owpc/pc/race/pay",
}

# 全角数字 → 半角
_JP_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")

BET_TYPE_MAP = {
    "3連単": "sanrentan",
    "3連複": "sanrenfuku",
    "2連単": "nirentan",
    "2連複": "nirenfuku",
    "拡連複": "kakurenfuku",
    "単勝": "tansho",
}


def _jp_int(s: str, default: int = 0) -> int:
    try:
        return int(str(s).translate(_JP_DIGIT).strip())
    except (ValueError, TypeError):
        return default


def _parse_race_time(s: str) -> float:
    """'1'49"9' → 109.9 秒"""
    m = re.match(r"(\d+)'(\d+)\"(\d)", str(s))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 10
    return 0.0


def _texts(td) -> list[str]:
    """td から空白行を除いたテキストリストを返す。"""
    return [x.strip() for x in td.get_text(separator="\n").split("\n") if x.strip()]


class BoatRaceScraper(BaseScraper):

    def __init__(self, config: dict):
        super().__init__(config)
        self._config = config

    def _url(self, key: str) -> str:
        return self.base_url + _URLS[key]

    def _params(self, stadium_code: str, race_date: date,
                race_no: Optional[int] = None) -> dict:
        p = {"jcd": f"{int(stadium_code):02d}", "hd": race_date.strftime("%Y%m%d")}
        if race_no is not None:
            p["rno"] = str(race_no)
        return p

    # ------------------------------------------------------------------
    # 開催場一覧
    # ------------------------------------------------------------------
    def get_holding_stadiums(self, race_date: date) -> list[str]:
        url = self._url("index")
        params = {"hd": race_date.strftime("%Y%m%d")}
        html = self._fetch_raw(url, params)
        soup = BeautifulSoup(html, "lxml")
        codes: list[str] = []
        for a in soup.select("a[href*='jcd=']"):
            m = re.search(r"jcd=(\d{2})", a.get("href", ""))
            if m:
                code = m.group(1)
                if code not in codes:
                    codes.append(code)
        logger.info(f"{race_date} 開催場: {codes}")
        return codes

    # ------------------------------------------------------------------
    # 出走表
    # ------------------------------------------------------------------
    def get_racelist(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("racelist"), self._params(stadium_code, race_date, race_no))
        return self._parse_racelist(html, stadium_code, race_date, race_no)

    # グレード（競走格付け）のみ。レディースは開催区分でありグレードではない。
    # 実際のCSSクラスは is-G1a, is-G3b のように大文字+サフィックス付きなので
    # プレフィックス一致（大文字小文字無視）で判定する。
    _GRADE_PREFIXES = [
        ("is-sg",    "SG"),
        ("is-pgi",   "PGI"),
        ("is-g1",    "G1"),
        ("is-g2",    "G2"),
        ("is-g3",    "G3"),
        ("is-ippan", "一般"),
    ]
    _LADIES_CLASSES = {"is-ladys", "is-ladies"}

    @classmethod
    def _grade_from_class(cls, css_class: str) -> str | None:
        cl = css_class.lower()
        for prefix, grade in cls._GRADE_PREFIXES:
            if cl == prefix or cl.startswith(prefix):
                return grade
        return None

    def _parse_race_header(self, soup) -> dict:
        """レースヘッダーからグレード・レース名・距離・レース種別を取得。"""
        grade = None
        title = None
        race_type = None
        distance = 1800
        is_night = False

        is_ladies = False
        h2 = soup.select_one(".heading2_title")
        if h2:
            for css_cls in h2.get("class", []):
                g = self._grade_from_class(css_cls)
                if g:
                    grade = g
                if css_cls.lower() in self._LADIES_CLASSES:
                    is_ladies = True
                if css_cls == "is-nighter":
                    is_night = True
            title = h2.get_text(strip=True)
            # タイトルテキストからもレディース判定（CSSクラスが付かない場合の補完）
            if title and ("レディース" in title or "クイーンズ" in title or "Lady" in title):
                is_ladies = True

        detail = soup.select_one(".title16_titleDetail__add2020")
        if detail:
            raw = detail.get_text(" ", strip=True).replace("　", " ")
            m = re.search(r"(\d+)m", raw)
            if m:
                distance = int(m.group(1))
            parts = [p for p in raw.split() if p and not re.fullmatch(r"\d+m?", p)]
            if parts:
                race_type = parts[0]

        if is_ladies and race_type:
            race_type = f"レディース/{race_type}"
        elif is_ladies:
            race_type = "レディース"

        return {
            "grade": grade,
            "title": title,
            "race_type": race_type,
            "distance": distance,
            "is_night": is_night,
        }

    def _parse_racelist(self, html: str, stadium_code: str,
                        race_date: date, race_no: int) -> pd.DataFrame:
        """
        出走表テーブル構造:
        - 艇ごとに 4 行 (rowspan=4 の td が先頭行を示す)
        - tr[0]: 枠番(rs=4) | 写真(rs=4) | 登番/名前/支部等(rs=4) |
                 F/L/ST(rs=4) | 全国成績(rs=4) | 当地成績(rs=4) |
                 モーター情報(rs=4) | ボート情報(rs=4) | ...
        """
        soup = BeautifulSoup(html, "lxml")
        race_header = self._parse_race_header(soup)

        tables = soup.find_all("table")
        # 出走表は table[1]（26〜27行）
        target = next((t for t in tables if len(t.find_all("tr")) > 15), None)
        if target is None:
            logger.warning(f"出走表テーブル見つからず: {stadium_code} {race_date} R{race_no}")
            return pd.DataFrame()

        rows = []
        all_trs = target.find_all("tr")

        for i, tr in enumerate(all_trs):
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue
            # 艇開始行の判定: td[0] が is-boatColor かつ rowspan=4
            cls0 = " ".join(tds[0].get("class", []))
            if "is-boatColor" not in cls0 or tds[0].get("rowspan") != "4":
                continue

            try:
                boat_no = _jp_int(tds[0].get_text(strip=True))

                # td[2]: 登番/クラス/名前/支部/年齢/体重
                info_td = tds[2] if len(tds) > 2 else None
                racer_no, racer_class, racer_name, age, weight = 0, "", "", 0, 0.0
                branch = ""
                if info_td:
                    fs11s = info_td.select("div.is-fs11")
                    if fs11s:
                        # "3392 / B1"
                        parts = fs11s[0].get_text(" ", strip=True).split("/")
                        racer_no = safe_int(parts[0].strip())
                        racer_class = parts[1].strip() if len(parts) > 1 else ""
                    name_div = info_td.select_one("div.is-fs18, div.is-fBold")
                    if name_div:
                        racer_name = name_div.get_text(strip=True)
                    if len(fs11s) > 1:
                        meta = fs11s[1].get_text(" ", strip=True)
                        # "三重/三重 60歳/51.5kg"
                        m = re.search(r"(\d+)歳", meta)
                        if m:
                            age = int(m.group(1))
                        m = re.search(r"([\d.]+)kg", meta)
                        if m:
                            weight = float(m.group(1))
                        branch_parts = meta.split()
                        if branch_parts:
                            branch = branch_parts[0].split("/")[0]

                # td[3]: F / L / 平均ST
                f_count, l_count, avg_st = 0, 0, 0.0
                if len(tds) > 3:
                    vals = _texts(tds[3])
                    if vals:
                        f_count = safe_int(vals[0].replace("F", "").replace("f", ""))
                    if len(vals) > 1:
                        l_count = safe_int(vals[1].replace("L", "").replace("l", ""))
                    if len(vals) > 2:
                        avg_st = safe_float(vals[2])

                # td[4]: 全国 勝率/2連/3連
                nw, n2, n3 = 0.0, 0.0, 0.0
                if len(tds) > 4:
                    vals = _texts(tds[4])
                    nw = safe_float(vals[0]) if vals else 0.0
                    n2 = safe_float(vals[1]) if len(vals) > 1 else 0.0
                    n3 = safe_float(vals[2]) if len(vals) > 2 else 0.0

                # td[5]: 当地 勝率/2連/3連
                lw, l2, l3 = 0.0, 0.0, 0.0
                if len(tds) > 5:
                    vals = _texts(tds[5])
                    lw = safe_float(vals[0]) if vals else 0.0
                    l2 = safe_float(vals[1]) if len(vals) > 1 else 0.0
                    l3 = safe_float(vals[2]) if len(vals) > 2 else 0.0

                # td[6]: モーター No/2連/3連
                motor_no, mot2, mot3 = 0, 0.0, 0.0
                if len(tds) > 6:
                    vals = _texts(tds[6])
                    motor_no = safe_int(vals[0]) if vals else 0
                    mot2 = safe_float(vals[1]) if len(vals) > 1 else 0.0
                    mot3 = safe_float(vals[2]) if len(vals) > 2 else 0.0

                # td[7]: ボート No/2連/3連
                boat_no_equip, boat2, boat3 = 0, 0.0, 0.0
                if len(tds) > 7:
                    vals = _texts(tds[7])
                    boat_no_equip = safe_int(vals[0]) if vals else 0
                    boat2 = safe_float(vals[1]) if len(vals) > 1 else 0.0
                    boat3 = safe_float(vals[2]) if len(vals) > 2 else 0.0

                rows.append({
                    "stadium_code": stadium_code,
                    "race_date": race_date,
                    "race_no": race_no,
                    "grade": race_header["grade"],
                    "race_type": race_header["race_type"],
                    "title": race_header["title"],
                    "distance": race_header["distance"],
                    "is_night": race_header["is_night"],
                    "boat_no": boat_no,
                    "racer_no": racer_no,
                    "racer_name": racer_name,
                    "racer_class": racer_class,
                    "branch": branch,
                    "age": age,
                    "weight": weight,
                    "f_count": f_count,
                    "l_count": l_count,
                    "avg_st": avg_st,
                    "national_win_rate": nw,
                    "national_top2_rate": n2,
                    "national_top3_rate": n3,
                    "local_win_rate": lw,
                    "local_top2_rate": l2,
                    "local_top3_rate": l3,
                    "motor_no": motor_no,
                    "motor_top2_rate": mot2,
                    "motor_top3_rate": mot3,
                    "boat_no_equipment": boat_no_equip,
                    "boat_top2_rate": boat2,
                    "boat_top3_rate": boat3,
                })
            except Exception as e:
                logger.warning(f"出走表パースエラー {stadium_code} R{race_no}: {e}")

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 直前情報 + 気象
    # ------------------------------------------------------------------
    def get_before_info(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("beforeinfo"), self._params(stadium_code, race_date, race_no))
        return self._parse_before_info(html, stadium_code, race_date, race_no)

    def get_weather(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("beforeinfo"), self._params(stadium_code, race_date, race_no))
        return self._parse_weather(html, stadium_code, race_date, race_no)

    def get_before_info_and_weather(self, stadium_code: str, race_date: date,
                                    race_no: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        """直前情報と気象を同一 HTML から取得（リクエスト節約）。"""
        html = self._fetch_raw(self._url("beforeinfo"), self._params(stadium_code, race_date, race_no))
        return (
            self._parse_before_info(html, stadium_code, race_date, race_no),
            self._parse_weather(html, stadium_code, race_date, race_no),
        )

    def _parse_before_info(self, html: str, stadium_code: str,
                           race_date: date, race_no: int) -> pd.DataFrame:
        """
        直前情報テーブル構造 (4行/艇):
        tr[0]: 枠番(rs=4) | 写真(rs=4) | 名前(rs=4) | 体重(rs=2) |
               展示T(rs=4) | チルト(rs=4) | ?(rs=4) | 部品交換(rs=4) | R | ...
        tr[1]: 進入 | <コース番号>
        tr[2]: <体重変化>(rs=2) | ST | <ST値>
        tr[3]: 着順 | <前走着順>
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        target = next((t for t in tables if len(t.find_all("tr")) > 15), None)
        if target is None:
            return pd.DataFrame()

        all_trs = target.find_all("tr")
        rows = []

        for i, tr in enumerate(all_trs):
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue
            cls0 = " ".join(tds[0].get("class", []))
            if "is-boatColor" not in cls0 or tds[0].get("rowspan") != "4":
                continue

            try:
                boat_no = _jp_int(tds[0].get_text(strip=True))

                exh_time = safe_float(tds[4].get_text(strip=True)) if len(tds) > 4 else 0.0
                tilt = safe_float(tds[5].get_text(strip=True)) if len(tds) > 5 else 0.0
                parts_text = tds[7].get_text(" ", strip=True) if len(tds) > 7 else ""
                propeller_changed = "プロペラ" in parts_text

                entry_course = 0
                exh_st = 0.0
                weight_diff = 0.0

                # tr[1]: 進入コース
                if i + 1 < len(all_trs):
                    tds1 = all_trs[i + 1].find_all("td", recursive=False)
                    if len(tds1) >= 2:
                        entry_course = safe_int(tds1[-1].get_text(strip=True))

                # tr[2]: 体重変化 / ST
                if i + 2 < len(all_trs):
                    tds2 = all_trs[i + 2].find_all("td", recursive=False)
                    if len(tds2) >= 3:
                        weight_diff = safe_float(tds2[0].get_text(strip=True))
                        exh_st = safe_float(tds2[2].get_text(strip=True))
                    elif len(tds2) == 2:
                        exh_st = safe_float(tds2[-1].get_text(strip=True))

                rows.append({
                    "stadium_code": stadium_code,
                    "race_date": race_date,
                    "race_no": race_no,
                    "boat_no": boat_no,
                    "entry_course": entry_course,
                    "exhibition_time": exh_time,
                    "exhibition_st": exh_st,
                    "tilt": tilt,
                    "propeller_changed": propeller_changed,
                    "parts_changed": parts_text,
                    "weight_diff": weight_diff,
                })
            except Exception as e:
                logger.warning(f"直前情報パースエラー {stadium_code} R{race_no}: {e}")

        return pd.DataFrame(rows)

    def _parse_weather(self, html: str, stadium_code: str,
                       race_date: date, race_no: int) -> pd.DataFrame:
        soup = BeautifulSoup(html, "lxml")
        weather_div = soup.find("div", class_="weather1")
        if not weather_div:
            return pd.DataFrame()

        result = {
            "stadium_code": stadium_code,
            "race_date": race_date,
            "race_no": race_no,
            "weather": "",
            "temperature": 0.0,
            "water_temperature": 0.0,
            "wind_speed": 0.0,
            "wind_direction": "",
            "wave_height": 0,
        }

        for unit in weather_div.select("div.weather1_bodyUnit"):
            cls = " ".join(unit.get("class", []))
            title_el = unit.select_one("span.weather1_bodyUnitLabelTitle")
            data_el = unit.select_one("span.weather1_bodyUnitLabelData")

            if "is-direction" in cls and "is-windDirection" not in cls:
                if data_el:
                    result["temperature"] = safe_float(
                        data_el.get_text().replace("℃", "").strip()
                    )
            elif "is-weather" in cls:
                if title_el:
                    result["weather"] = title_el.get_text(strip=True)
            elif "is-wind" in cls and "is-windDirection" not in cls:
                if data_el:
                    result["wind_speed"] = safe_float(
                        data_el.get_text().replace("m", "").strip()
                    )
            elif "is-windDirection" in cls:
                img = unit.select_one("p.weather1_bodyUnitImage")
                if img:
                    m = re.search(r"is-wind(\d+)", " ".join(img.get("class", [])))
                    if m:
                        result["wind_direction"] = m.group(1)
            elif "is-waterTemperature" in cls:
                if data_el:
                    result["water_temperature"] = safe_float(
                        data_el.get_text().replace("℃", "").strip()
                    )
            elif "is-wave" in cls or "is-waveHeight" in cls:
                if data_el:
                    result["wave_height"] = safe_int(
                        data_el.get_text().replace("cm", "").strip()
                    )

        return pd.DataFrame([result])

    # ------------------------------------------------------------------
    # 3連単オッズ
    # ------------------------------------------------------------------
    def get_odds_sanrentan(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("odds3t"), self._params(stadium_code, race_date, race_no))
        return self._parse_odds_sanrentan(html, stadium_code, race_date, race_no)

    def _parse_odds_sanrentan(self, html: str, stadium_code: str,
                              race_date: date, race_no: int) -> pd.DataFrame:
        """
        3連単オッズページ構造:
        - thead: 6列 (boatColor1〜6 が 1着)
        - tbody: 20行 × 6列グループ
          - 新2着グループ開始行: [2着(rowspan=4), 3着, odds] × 6列
          - 継続行:              [3着, odds] × 6列
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        target = next((t for t in tables if len(t.find_all("td", class_="oddsPoint")) >= 100), None)
        if target is None:
            logger.warning(f"3連単オッズテーブル見つからず: {stadium_code} R{race_no}")
            return pd.DataFrame()

        # 1着の順番を thead から取得
        ichaku_boats = []
        thead = target.find("thead")
        if thead:
            for th in thead.find_all("th"):
                txt = th.get_text(strip=True)
                if txt.isdigit() and "is-boatColor" in " ".join(th.get("class", [])):
                    ichaku_boats.append(int(txt))
        if not ichaku_boats:
            ichaku_boats = [1, 2, 3, 4, 5, 6]

        n_cols = len(ichaku_boats)
        current_niban = [None] * n_cols
        remaining = [0] * n_cols  # 現在の2着グループの残り行数

        rows = []
        tbody = target.find("tbody")
        if not tbody:
            return pd.DataFrame()

        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            col_offset = 0

            for col in range(n_cols):
                if col_offset >= len(tds):
                    break

                if remaining[col] == 0:
                    # 新グループ: [2着(rowspan), 3着, odds] の 3セル
                    if col_offset + 2 >= len(tds):
                        break
                    td_niban = tds[col_offset]
                    td_sanban = tds[col_offset + 1]
                    td_odds = tds[col_offset + 2]
                    col_offset += 3
                    rowspan = int(td_niban.get("rowspan", 1))
                    current_niban[col] = safe_int(td_niban.get_text(strip=True))
                    remaining[col] = rowspan - 1
                else:
                    # 継続: [3着, odds] の 2セル
                    if col_offset + 1 >= len(tds):
                        break
                    td_sanban = tds[col_offset]
                    td_odds = tds[col_offset + 1]
                    col_offset += 2
                    remaining[col] -= 1

                odds = safe_float(td_odds.get_text(strip=True))
                sanban = safe_int(td_sanban.get_text(strip=True))
                if odds > 0 and sanban > 0 and current_niban[col]:
                    rows.append({
                        "stadium_code": stadium_code,
                        "race_date": race_date,
                        "race_no": race_no,
                        "bet_type": "sanrentan",
                        "combination": f"{ichaku_boats[col]}-{current_niban[col]}-{sanban}",
                        "odds": odds,
                    })

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 3連複オッズ
    # ------------------------------------------------------------------
    def get_odds_sanrenfuku(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("odds3f"), self._params(stadium_code, race_date, race_no))
        return self._parse_odds_sanrenfuku(html, stadium_code, race_date, race_no)

    def _parse_odds_sanrenfuku(self, html: str, stadium_code: str,
                               race_date: date, race_no: int) -> pd.DataFrame:
        """
        3連複オッズページ構造:
        - thead: 6列 (各列 = base_boat = 組み合わせの最小艇番)
        - tbody: 10行 × 6列グループ
          - 各列グループ: [2nd(rowspan), 3rd, odds] または is-disabled でスキップ
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        target = next((t for t in tables if len(t.find_all("td", class_="oddsPoint")) >= 15), None)
        if target is None:
            logger.warning(f"3連複オッズテーブル見つからず: {stadium_code} R{race_no}")
            return pd.DataFrame()

        base_boats = []
        thead = target.find("thead")
        if thead:
            for th in thead.find_all("th"):
                txt = th.get_text(strip=True)
                if txt.isdigit() and "is-boatColor" in " ".join(th.get("class", [])):
                    base_boats.append(int(txt))
        if not base_boats:
            base_boats = [1, 2, 3, 4, 5, 6]

        n_cols = len(base_boats)
        current_second = [None] * n_cols
        remaining = [0] * n_cols
        active = [False] * n_cols

        rows = []
        tbody = target.find("tbody")
        if not tbody:
            return pd.DataFrame()

        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            col_offset = 0

            for col in range(n_cols):
                if col_offset >= len(tds):
                    break

                if remaining[col] == 0:
                    if col_offset + 2 >= len(tds):
                        break
                    td_sec = tds[col_offset]
                    td_third = tds[col_offset + 1]
                    td_odds = tds[col_offset + 2]
                    col_offset += 3
                    rowspan = int(td_sec.get("rowspan", 1))
                    remaining[col] = rowspan - 1
                    disabled = "is-disabled" in " ".join(td_sec.get("class", []))
                    active[col] = not disabled
                    if not disabled:
                        current_second[col] = safe_int(td_sec.get_text(strip=True))
                    else:
                        current_second[col] = None
                else:
                    if col_offset + 1 >= len(tds):
                        break
                    td_third = tds[col_offset]
                    td_odds = tds[col_offset + 1]
                    col_offset += 2
                    remaining[col] -= 1

                if not active[col]:
                    continue

                third = safe_int(td_third.get_text(strip=True))
                odds = safe_float(td_odds.get_text(strip=True))
                if odds > 0 and third > 0 and current_second[col]:
                    combo = sorted([base_boats[col], current_second[col], third])
                    rows.append({
                        "stadium_code": stadium_code,
                        "race_date": race_date,
                        "race_no": race_no,
                        "bet_type": "sanrenfuku",
                        "combination": f"{combo[0]}-{combo[1]}-{combo[2]}",
                        "odds": odds,
                    })

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 2連単 / 2連複オッズ
    # ------------------------------------------------------------------
    def get_odds_tansho(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("oddstf"), self._params(stadium_code, race_date, race_no))
        return self._parse_odds_tansho(html, stadium_code, race_date, race_no)

    def _parse_odds_tansho(self, html: str, stadium_code: str,
                            race_date: date, race_no: int) -> pd.DataFrame:
        """単勝: oddstf の table[1]。各行 [艇番, 選手名, 単勝オッズ]"""
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        if len(tables) < 2:
            return pd.DataFrame()
        # table[1] = 単勝オッズ表
        rows = []
        for tr in tables[1].find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            boat_txt = tds[0].get_text(strip=True)
            odds_txt = tds[2].get_text(strip=True)
            if not boat_txt.isdigit():
                continue
            boat_no = int(boat_txt)
            try:
                odds = float(odds_txt)
            except (ValueError, TypeError):
                continue
            rows.append({
                "stadium_code": stadium_code,
                "race_date": race_date,
                "race_no": race_no,
                "bet_type": "tansho",
                "combination": str(boat_no),
                "odds": odds,
            })
        return pd.DataFrame(rows)

    def get_odds_nirentan(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("odds2tf"), self._params(stadium_code, race_date, race_no))
        return self._parse_odds_nirentan(html, stadium_code, race_date, race_no)

    def get_odds_nirenfuku(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("odds2tf"), self._params(stadium_code, race_date, race_no))
        return self._parse_odds_nirenfuku(html, stadium_code, race_date, race_no)

    def _parse_odds_nirentan(self, html: str, stadium_code: str,
                             race_date: date, race_no: int) -> pd.DataFrame:
        """
        2連単: odds2tf の table[1]
        - 5行 × 6列グループ、各グループ: [2着, odds] (rowspan なし)
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        # table[1] = 2連単
        t2t = next((t for t in tables if len(t.find_all("td", class_="oddsPoint")) == 30), None)
        if t2t is None:
            return pd.DataFrame()

        ichaku_boats = []
        thead = t2t.find("thead")
        if thead:
            for th in thead.find_all("th"):
                txt = th.get_text(strip=True)
                if txt.isdigit():
                    ichaku_boats.append(int(txt))
        if not ichaku_boats:
            ichaku_boats = [1, 2, 3, 4, 5, 6]

        rows = []
        tbody = t2t.find("tbody")
        if not tbody:
            return pd.DataFrame()

        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            for col, ichaku in enumerate(ichaku_boats):
                col_offset = col * 2
                if col_offset + 1 >= len(tds):
                    break
                td_boat = tds[col_offset]
                td_odds = tds[col_offset + 1]
                if "is-disabled" in " ".join(td_boat.get("class", [])):
                    continue
                niban = safe_int(td_boat.get_text(strip=True))
                odds = safe_float(td_odds.get_text(strip=True))
                if odds > 0 and niban > 0:
                    rows.append({
                        "stadium_code": stadium_code,
                        "race_date": race_date,
                        "race_no": race_no,
                        "bet_type": "nirentan",
                        "combination": f"{ichaku}-{niban}",
                        "odds": odds,
                    })

        return pd.DataFrame(rows)

    def _parse_odds_nirenfuku(self, html: str, stadium_code: str,
                              race_date: date, race_no: int) -> pd.DataFrame:
        """
        2連複: odds2tf の table[2]
        - 5行 × 6列グループ、各グループ: [2nd, odds]、is-disabled でスキップ
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        t2f = next((t for t in tables if len(t.find_all("td", class_="oddsPoint")) == 15), None)
        if t2f is None:
            return pd.DataFrame()

        base_boats = []
        thead = t2f.find("thead")
        if thead:
            for th in thead.find_all("th"):
                txt = th.get_text(strip=True)
                if txt.isdigit():
                    base_boats.append(int(txt))
        if not base_boats:
            base_boats = [1, 2, 3, 4, 5, 6]

        rows = []
        tbody = t2f.find("tbody")
        if not tbody:
            return pd.DataFrame()

        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            for col, base in enumerate(base_boats):
                col_offset = col * 2
                if col_offset + 1 >= len(tds):
                    break
                td_boat = tds[col_offset]
                td_odds = tds[col_offset + 1]
                if "is-disabled" in " ".join(td_boat.get("class", [])):
                    continue
                second = safe_int(td_boat.get_text(strip=True))
                odds = safe_float(td_odds.get_text(strip=True))
                if odds > 0 and second > 0:
                    a, b = min(base, second), max(base, second)
                    rows.append({
                        "stadium_code": stadium_code,
                        "race_date": race_date,
                        "race_no": race_no,
                        "bet_type": "nirenfuku",
                        "combination": f"{a}-{b}",
                        "odds": odds,
                    })

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # レース結果 + 払戻
    # ------------------------------------------------------------------
    def get_race_result(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("raceresult"), self._params(stadium_code, race_date, race_no))
        return self._parse_race_result(html, stadium_code, race_date, race_no)

    def get_payouts(self, stadium_code: str, race_date: date, race_no: int) -> pd.DataFrame:
        html = self._fetch_raw(self._url("raceresult"), self._params(stadium_code, race_date, race_no))
        return self._parse_payouts(html, stadium_code, race_date, race_no)

    def get_race_result_and_payouts(self, stadium_code: str, race_date: date,
                                    race_no: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        html = self._fetch_raw(self._url("raceresult"), self._params(stadium_code, race_date, race_no))
        return (
            self._parse_race_result(html, stadium_code, race_date, race_no),
            self._parse_payouts(html, stadium_code, race_date, race_no),
        )

    def parse_pay_summary(self, html: str) -> list[tuple[str, int]]:
        """払戻一覧ページから終了済みレースの (venue_code, race_no) リストを返す。"""
        soup = BeautifulSoup(html, "lxml")
        seen: set[tuple[str, int]] = set()
        results: list[tuple[str, int]] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "raceresult" not in href:
                continue
            m_jcd = re.search(r"jcd=(\d+)", href)
            m_rno = re.search(r"rno=(\d+)", href)
            if m_jcd and m_rno:
                key = (m_jcd.group(1), int(m_rno.group(1)))
                if key not in seen:
                    seen.add(key)
                    results.append(key)
        return sorted(results)

    def collect_day_results(self, race_date: date, max_workers: int = 5) -> dict:
        """払戻一覧ページから終了済みレースを特定し、結果・払戻のみ収集する。"""
        pay_params = {"hd": race_date.strftime("%Y%m%d")}
        html = self._fetch_raw(self._url("pay"), pay_params)
        finished = self.parse_pay_summary(html)
        logger.info(f"終了済みレース: {len(finished)}件")

        merged: dict[str, list] = {"race_result": [], "payouts": []}

        if max_workers <= 1:
            for venue_code, race_no in finished:
                try:
                    rr, py = self.get_race_result_and_payouts(venue_code, race_date, race_no)
                    merged["race_result"].append(rr)
                    merged["payouts"].append(py)
                except Exception as e:
                    logger.warning(f"結果取得失敗 {venue_code} R{race_no}: {e}")
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            config = self._config

            def _worker(venue_code: str, race_no: int) -> tuple[pd.DataFrame, pd.DataFrame]:
                with BoatRaceScraper(config) as s:
                    return s.get_race_result_and_payouts(venue_code, race_date, race_no)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_worker, vc, rn): (vc, rn) for vc, rn in finished}
                for future in as_completed(futures):
                    vc, rn = futures[future]
                    try:
                        rr, py = future.result()
                        merged["race_result"].append(rr)
                        merged["payouts"].append(py)
                    except Exception as e:
                        logger.warning(f"結果取得失敗 {vc} R{rn}: {e}")

        return {
            k: pd.concat(v, ignore_index=True)
            for k, v in merged.items()
            if v and any(not df.empty for df in v)
        }

    def _parse_race_result(self, html: str, stadium_code: str,
                           race_date: date, race_no: int) -> pd.DataFrame:
        """
        着順テーブル (table[1]):
        td[0]=着順(JP), td[1]=艇番(colored), td[2]=登番/名前, td[3]=タイム
        """
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        # 着順テーブル: td.is-boatColor を含む 6〜7行のテーブル
        target = None
        for t in tables:
            tds = t.select("td[class*='is-boatColor']")
            if len(tds) >= 6:
                target = t
                break
        if target is None:
            return pd.DataFrame()

        rows = []
        for tr in target.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 3:
                continue
            try:
                arrival = _jp_int(tds[0].get_text(strip=True))
                boat_no = safe_int(tds[1].get_text(strip=True))
                if arrival == 0 or boat_no == 0:
                    continue
                racer_span = tds[2].select_one("span.is-fs12")
                racer_no = safe_int(racer_span.get_text(strip=True)) if racer_span else 0
                race_time = _parse_race_time(tds[3].get_text(strip=True)) if len(tds) > 3 else 0.0
                rows.append({
                    "stadium_code": stadium_code,
                    "race_date": race_date,
                    "race_no": race_no,
                    "arrival_order": arrival,
                    "boat_no": boat_no,
                    "racer_no": racer_no,
                    "race_time": race_time,
                })
            except Exception as e:
                logger.warning(f"着順パースエラー: {e}")

        return pd.DataFrame(rows)

    def _parse_payouts(self, html: str, stadium_code: str,
                       race_date: date, race_no: int) -> pd.DataFrame:
        """
        払戻テーブル:
        <td rowspan=2>3連単</td> | <combo numberSet1> | <payout is-payout1>
        """
        soup = BeautifulSoup(html, "lxml")
        rows = []
        current_bet_type = ""

        for tr in soup.find_all("tr", class_="is-p3-0"):
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue

            # bet_type セル（rowspan=2 があれば新しい賭け式）
            if tds[0].get("rowspan"):
                current_bet_type = tds[0].get_text(strip=True)
                combo_td = tds[1] if len(tds) > 1 else None
                payout_td = tds[2] if len(tds) > 2 else None
            else:
                # 2行目（同一賭け式の2番目の組み合わせ）
                combo_td = tds[0] if tds else None
                payout_td = tds[1] if len(tds) > 1 else None

            if not combo_td or not payout_td:
                continue

            # 組み合わせの数字を抽出
            nums = combo_td.select("span.numberSet1_number")
            if not nums:
                continue
            combination = "-".join(n.get_text(strip=True) for n in nums)
            if not combination:
                continue

            # 払戻金額
            payout_text = payout_td.get_text(strip=True).replace("¥", "").replace(",", "")
            payout = safe_int(payout_text)

            db_type = BET_TYPE_MAP.get(current_bet_type, current_bet_type)
            rows.append({
                "stadium_code": stadium_code,
                "race_date": race_date,
                "race_no": race_no,
                "bet_type": db_type,
                "combination": combination,
                "payout": payout,
            })

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 1日分まとめて取得
    # ------------------------------------------------------------------
    def _collect_one_stadium(self, race_date: date, code: str,
                             skip_odds: bool = False,
                             skip_before_info: bool = True) -> dict[str, list]:
        """1場分の全レース（R1〜R12）を収集してバケツ辞書を返す。

        skip_odds=True のとき 3連単/3連複/2連単/複 をスキップする（歴史データ収集用）。
        skip_before_info=True のとき直前情報・気象をスキップする（朝一括収集用）。
        """
        buckets: dict[str, list] = {
            "racelist": [], "before_info": [], "weather": [],
            "odds_sanrentan": [], "odds_sanrenfuku": [],
            "odds_nirentan": [], "odds_nirenfuku": [],
            "odds_tansho": [],
            "race_result": [], "payouts": [],
        }
        for rno in range(1, 13):
            params = (code, race_date, rno)
            try:
                buckets["racelist"].append(self.get_racelist(*params))
            except Exception as e:
                logger.warning(f"出走表取得失敗 {code} R{rno}: {e}")
            if not skip_before_info:
                try:
                    bi, wt = self.get_before_info_and_weather(*params)
                    buckets["before_info"].append(bi)
                    buckets["weather"].append(wt)
                except Exception as e:
                    logger.warning(f"直前/気象取得失敗 {code} R{rno}: {e}")
            if not skip_odds:
                try:
                    buckets["odds_sanrentan"].append(self.get_odds_sanrentan(*params))
                except Exception as e:
                    logger.warning(f"3連単取得失敗 {code} R{rno}: {e}")
                try:
                    buckets["odds_sanrenfuku"].append(self.get_odds_sanrenfuku(*params))
                except Exception as e:
                    logger.warning(f"3連複取得失敗 {code} R{rno}: {e}")
                try:
                    html2 = self._fetch_raw(self._url("odds2tf"), self._params(*params))
                    buckets["odds_nirentan"].append(self._parse_odds_nirentan(html2, *params))
                    buckets["odds_nirenfuku"].append(self._parse_odds_nirenfuku(html2, *params))
                except Exception as e:
                    logger.warning(f"2連オッズ取得失敗 {code} R{rno}: {e}")
                try:
                    buckets["odds_tansho"].append(self.get_odds_tansho(*params))
                except Exception as e:
                    logger.warning(f"単勝取得失敗 {code} R{rno}: {e}")
            try:
                rr, py = self.get_race_result_and_payouts(*params)
                buckets["race_result"].append(rr)
                buckets["payouts"].append(py)
            except Exception as e:
                logger.warning(f"結果/払戻取得失敗 {code} R{rno}: {e}")
        return buckets

    def collect_day(self, race_date: date,
                    stadium_codes: Optional[list[str]] = None,
                    max_workers: int = 1,
                    skip_odds: bool = False,
                    skip_before_info: bool = True) -> dict:
        """1日分の全場・全レースデータを取得して DataFrame 辞書で返す。

        max_workers > 1 のとき各場を並列フェッチする（場ごとに独立セッション）。
        skip_odds=True のときオッズ4種をスキップ（歴史データ収集で使用）。
        skip_before_info=True のとき直前情報・気象をスキップ（デフォルト）。
        """
        if stadium_codes is None:
            try:
                stadium_codes = self.get_holding_stadiums(race_date)
            except Exception as e:
                logger.error(f"開催場取得失敗: {e}")
                return {}

        merged: dict[str, list] = {
            "racelist": [], "before_info": [], "weather": [],
            "odds_sanrentan": [], "odds_sanrenfuku": [],
            "odds_nirentan": [], "odds_nirenfuku": [],
            "odds_tansho": [],
            "race_result": [], "payouts": [],
        }

        if max_workers <= 1:
            for code in stadium_codes:
                for k, v in self._collect_one_stadium(race_date, code, skip_odds, skip_before_info).items():
                    merged[k].extend(v)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            config = self._config

            def _worker(code: str) -> dict[str, list]:
                with BoatRaceScraper(config) as s:
                    return s._collect_one_stadium(race_date, code, skip_odds, skip_before_info)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_worker, code): code for code in stadium_codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        for k, v in future.result().items():
                            merged[k].extend(v)
                    except Exception as e:
                        logger.error(f"場{code} 並列収集失敗: {e}")

        return {
            k: pd.concat(v, ignore_index=True)
            for k, v in merged.items()
            if v and any(not df.empty for df in v)
        }
