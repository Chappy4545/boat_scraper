"""SQLAlchemy ORM モデル — 13テーブル定義"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, Boolean, DateTime, Date,
    ForeignKey, UniqueConstraint, Index, Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Stadium(Base):
    """レース場マスタ"""
    __tablename__ = "stadiums"

    id = Column(Integer, primary_key=True)
    code = Column(String(2), unique=True, nullable=False)   # "01"〜"24"
    name = Column(String(20), nullable=False)
    location = Column(String(50))
    water_type = Column(String(10))     # 淡水 / 海水
    tidal_diff = Column(Float)          # 干満差(m)
    # 場別コース成績（1〜6コース）
    course1_win_rate = Column(Float)
    course2_win_rate = Column(Float)
    course3_win_rate = Column(Float)
    course4_win_rate = Column(Float)
    course5_win_rate = Column(Float)
    course6_win_rate = Column(Float)
    course1_top2_rate = Column(Float)
    course2_top2_rate = Column(Float)
    course3_top2_rate = Column(Float)
    course4_top2_rate = Column(Float)
    course5_top2_rate = Column(Float)
    course6_top2_rate = Column(Float)
    course1_top3_rate = Column(Float)
    course2_top3_rate = Column(Float)
    course3_top3_rate = Column(Float)
    course4_top3_rate = Column(Float)
    course5_top3_rate = Column(Float)
    course6_top3_rate = Column(Float)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    races = relationship("Race", back_populates="stadium")


# 2026-08-21 に RacerMaster / Motor / Boat を削除した。
# 3テーブルとも作られてから一度も書き込まれておらず（全部0行）、
# 定義以外にコード上の参照も無かった。選手・モーター・ボートの成績は
# 出走表ページに印刷されており、race_entries に入っている。
# マスタとして別に持つ設計だったが、そちらは使われないまま残っていた。


class Race(Base):
    """レース基本情報"""
    __tablename__ = "races"

    id = Column(Integer, primary_key=True)
    race_date = Column(Date, nullable=False)
    stadium_id = Column(Integer, ForeignKey("stadiums.id"), nullable=False)
    race_no = Column(Integer, nullable=False)   # 1〜12
    grade = Column(String(10))                  # SG/G1/G2/G3/一般
    race_type = Column(String(10))              # 予選/準優/優勝
    title = Column(String(100))
    closing_time = Column(String(10))           # 締切時刻 "15:30"
    is_night = Column(Boolean, default=False)
    distance = Column(Integer, default=1800)    # レース距離(m)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("race_date", "stadium_id", "race_no"),
        Index("ix_races_date_stadium", "race_date", "stadium_id"),
    )

    stadium = relationship("Stadium", back_populates="races")
    entries = relationship("RaceEntry", back_populates="race")
    results = relationship("RaceResult", back_populates="race")
    weather = relationship("Weather", back_populates="race", uselist=False)
    odds = relationship("Odds", back_populates="race")
    before_info = relationship("BeforeInfo", back_populates="race")
    payouts = relationship("Payout", back_populates="race")
    predictions = relationship("Prediction", back_populates="race")
    bets = relationship("Bet", back_populates="race")


class RaceEntry(Base):
    """出走表（艇・選手情報）"""
    __tablename__ = "race_entries"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    boat_no = Column(Integer, nullable=False)       # 枠番 1〜6
    racer_no = Column(Integer, nullable=False)      # 登録番号
    racer_name = Column(String(20))
    racer_class = Column(String(5))
    branch = Column(String(10))
    age = Column(Integer)
    weight = Column(Float)
    f_count = Column(Integer, default=0)
    l_count = Column(Integer, default=0)
    avg_st = Column(Float)
    national_win_rate = Column(Float)
    national_top2_rate = Column(Float)
    national_top3_rate = Column(Float)
    local_win_rate = Column(Float)
    local_top2_rate = Column(Float)
    local_top3_rate = Column(Float)
    motor_no = Column(Integer)
    motor_top2_rate = Column(Float)
    motor_top3_rate = Column(Float)
    boat_no_equipment = Column(Integer)             # ボート番号
    boat_top2_rate = Column(Float)
    boat_top3_rate = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("race_id", "boat_no"),)

    race = relationship("Race", back_populates="entries")


class BeforeInfo(Base):
    """直前情報（展示タイム等）"""
    __tablename__ = "before_info"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    boat_no = Column(Integer, nullable=False)
    entry_course = Column(Integer)              # 進入コース
    exhibition_time = Column(Float)             # 展示タイム
    exhibition_st = Column(Float)               # 展示ST
    exhibition_rank = Column(Integer)           # 展示順位
    tilt = Column(Float)                        # チルト角
    propeller_changed = Column(Boolean, default=False)
    parts_changed = Column(Text)                # 部品交換内容
    weight_diff = Column(Float)                 # 体重変化(kg)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("race_id", "boat_no"),)

    race = relationship("Race", back_populates="before_info")


class Weather(Base):
    """気象・水面情報"""
    __tablename__ = "weather"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), unique=True, nullable=False)
    weather = Column(String(20))        # 晴/曇/雨 等
    temperature = Column(Float)         # 気温(℃)
    water_temperature = Column(Float)   # 水温(℃)
    wind_direction = Column(String(10)) # 風向
    wind_speed = Column(Float)          # 風速(m/s)
    wave_height = Column(Integer)       # 波高(cm)
    recorded_at = Column(DateTime)

    race = relationship("Race", back_populates="weather")


class Odds(Base):
    """オッズ"""
    __tablename__ = "odds"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    # bet_type: tansho/fukusho/nirentan/nirenfuku/kakurenfuku/sanrentan/sanrenfuku
    bet_type = Column(String(20), nullable=False)
    combination = Column(String(20), nullable=False)    # "1-2-3" 形式
    odds = Column(Float)
    # 複勝と拡連複は「誰と一緒に入るか」で配当が変わるので、板は `1.0-1.2` の
    # ような**範囲**で出る。odds に下限、odds_upper に上限を入れる。
    # 範囲でない賭式では None。
    #
    # 2026-09-03: それまで下限しか保存しておらず、上限は毎日捨てていた。
    # オッズは遡って取れないので、捨てた分は永久に戻らない
    # （[[project_odds_are_perishable]]）。実測では拡連複の当たり269本のうち
    # 55.0% が下限より高く払っており、下限だけでは EV が系統的に低く出る。
    #
    # ⚠️ 上限があっても「元返しかどうか」は買う時点では決まらない。
    # 相方の艇で決まるため。範囲の幅は情報であって予告ではない。
    odds_upper = Column(Float)
    is_final = Column(Boolean, default=False)           # 確定オッズか
    # レース当日に取得した値か（= 買う時点で実際に見られた値か）。
    # 2026-08-11: 全レコードが is_final=1 で区別できず、レース後に遡及取得した
    # 確定オッズを「買い目を選ぶ材料」として検証に使っていた。知り得ない値で
    # 選べば成績は当然良く見える（7-8月の検証データは83%がこれだった）。
    # 検証では必ず is_live=True のみを使うこと。
    is_live = Column(Boolean, default=False)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("race_id", "bet_type", "combination", "is_final"),
        Index("ix_odds_race_type", "race_id", "bet_type"),
    )

    race = relationship("Race", back_populates="odds")


class RaceResult(Base):
    """レース結果（着順）"""
    __tablename__ = "race_results"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    arrival_order = Column(Integer, nullable=False)     # 着順
    boat_no = Column(Integer, nullable=False)           # 枠番
    racer_no = Column(Integer)
    race_time = Column(Float)                           # タイム(秒)
    st_time = Column(Float)                             # スタートタイム
    entry_course = Column(Integer)                      # 実際の進入コース
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("race_id", "arrival_order"),)

    race = relationship("Race", back_populates="results")


class Payout(Base):
    """払戻金"""
    __tablename__ = "payouts"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    bet_type = Column(String(20), nullable=False)
    combination = Column(String(20), nullable=False)
    payout = Column(Integer)                    # 払戻金(円)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("race_id", "bet_type", "combination"),)

    race = relationship("Race", back_populates="payouts")


class Prediction(Base):
    """モデル予測結果（艇ごとの着順確率）"""
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    model_version = Column(String(20), nullable=False)
    boat_no = Column(Integer, nullable=False)
    win_prob = Column(Float)        # 1着確率
    top2_prob = Column(Float)       # 2着以内確率
    top3_prob = Column(Float)       # 3着以内確率
    confidence = Column(Float)      # モデル信頼度
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("race_id", "model_version", "boat_no"),)

    race = relationship("Race", back_populates="predictions")


class Bet(Base):
    """推奨買い目（期待値計算結果）"""
    __tablename__ = "bets"

    id = Column(Integer, primary_key=True)
    race_id = Column(Integer, ForeignKey("races.id"), nullable=False)
    model_version = Column(String(20), nullable=False)
    bet_type = Column(String(20), nullable=False)       # sanrentan 等
    combination = Column(String(20), nullable=False)    # "1-2-3"
    model_prob = Column(Float)                          # モデル推定確率
    # 市場の含意確率（1/オッズ を全組合せで正規化＝控除率を除いた市場の見立て）。
    # モデル確率 ÷ market_prob が「市場とどれだけ食い違うか」= edge。
    # 2026-08-11 の検証で、両期間とも利益は edge が大きい買い目に集中しており
    # 市場も高く評価している買い目は一貫して負けていた（70%台）。
    # 採用可否を後から検証できるよう、買う買わないに関わらず記録する。
    market_prob = Column(Float)
    odds = Column(Float)                                # オッズ
    expected_value = Column(Float)                      # 期待値
    recommended_amount = Column(Integer)                # 推奨賭け金(円)
    is_pass = Column(Boolean, default=False)            # 見送り
    pass_reason = Column(String(100))                   # 見送り理由
    # 締切が近づいた時点で固定した「これを買えばよい」買い目か。
    # 朝の買い目は目安にすぎず、オッズが動けば EV も変わる。日中ずっと
    # 更新され続けると「いつ買えばいいのか」が分からないため、
    # 締切前に一度確定させ、以後は更新しない。
    is_final_pick = Column(Boolean, default=False)
    is_hit = Column(Boolean)                            # 的中（結果判明後）
    actual_payout = Column(Integer)                     # 実際の払戻
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_bets_race_type", "race_id", "bet_type"),)

    race = relationship("Race", back_populates="bets")


class BacktestResult(Base):
    """バックテスト集計結果"""
    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True)
    run_at = Column(DateTime, default=datetime.utcnow)
    model_version = Column(String(20), nullable=False)
    date_start = Column(Date, nullable=False)
    date_end = Column(Date, nullable=False)
    total_races = Column(Integer)
    bet_races = Column(Integer)
    pass_races = Column(Integer)
    total_bets = Column(Integer)
    hits = Column(Integer)
    hit_rate = Column(Float)
    total_investment = Column(Integer)
    total_return = Column(Integer)
    roi = Column(Float)             # 回収率
    max_drawdown = Column(Float)
    max_consecutive_losses = Column(Integer)
    avg_odds = Column(Float)
    summary_json = Column(Text)     # 月別・場別等の詳細JSON
