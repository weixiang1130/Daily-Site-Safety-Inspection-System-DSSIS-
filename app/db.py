# -*- coding: utf-8 -*-
"""資料模型與連線設定。

預設連 SQL Server LocalDB（本機開發），與未來公司內網的 SQL Server 同一種資料庫，
因此開發期間寫的 DDL、查詢、型別行為與正式環境一致，遷移時不需重新驗證。

連線來源（優先序）：
  1. 環境變數 DATABASE_URL —— 直接指定完整連線字串
  2. 環境變數 DB_BACKEND=sqlite —— 退回 SQLite（無 SQL Server 的環境用）
  3. 預設 —— SQL Server LocalDB 的 SafetyOps 資料庫

搬到公司內網時只需設定：
  DATABASE_URL=mssql+pyodbc://user:pw@SRV-DB01/SafetyOps?driver=ODBC+Driver+17+for+SQL+Server

所有中文欄位一律使用 Unicode / UnicodeText，在 SQL Server 上對應 NVARCHAR /
NVARCHAR(MAX)，避免非 Unicode 定序造成中文亂碼。
"""
import os
import urllib.parse
from datetime import date, datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, Unicode,
    UnicodeText, UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# 連線字串
# --------------------------------------------------------------------------
MSSQL_SERVER = os.environ.get("MSSQL_SERVER", r"(localdb)\MSSQLLocalDB")
MSSQL_DATABASE = os.environ.get("MSSQL_DATABASE", "SafetyOps")
MSSQL_DRIVER = os.environ.get("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")


def _localdb_url() -> str:
    odbc = (f"DRIVER={{{MSSQL_DRIVER}}};SERVER={MSSQL_SERVER};"
            f"DATABASE={MSSQL_DATABASE};Trusted_Connection=yes;"
            f"TrustServerCertificate=yes;")
    return "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc)


def _sqlite_url() -> str:
    return "sqlite:///" + os.path.join(BASE_DIR, "safety.db").replace("\\", "/")


def _normalize(url: str) -> str:
    """雲端平台常給 postgres:// 開頭的連線字串，SQLAlchemy 2.x 不再接受。"""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


if os.environ.get("DATABASE_URL"):
    DATABASE_URL = _normalize(os.environ["DATABASE_URL"])
elif os.environ.get("DB_BACKEND", "mssql").lower() == "sqlite":
    DATABASE_URL = _sqlite_url()
else:
    DATABASE_URL = _localdb_url()

IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_MSSQL = DATABASE_URL.startswith("mssql")

_engine_kwargs = {"echo": False, "pool_pre_ping": True}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
elif IS_MSSQL:
    # pyodbc 批次寫入加速，僅 mssql+pyodbc 支援，其他方言傳入會直接報錯
    _engine_kwargs["fast_executemany"] = True

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


# --------------------------------------------------------------------------
# 主檔
# --------------------------------------------------------------------------
class Site(Base):
    """工地。"""
    __tablename__ = "sites"
    id = Column(Integer, primary_key=True)
    code = Column(Unicode(32), unique=True, nullable=False)
    name = Column(Unicode(128), nullable=False)
    active = Column(Boolean, default=True)


class Vendor(Base):
    """責任廠商（供應商 / 協力商）。"""
    __tablename__ = "vendors"
    id = Column(Integer, primary_key=True)
    code = Column(Unicode(32), unique=True, nullable=False)
    name = Column(Unicode(128), nullable=False)
    active = Column(Boolean, default=True)


class User(Base):
    """使用者。原型階段用帳密，日後可換成公司 AD/SSO，只需改 auth.py。"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(Unicode(64), unique=True, nullable=False)
    password_hash = Column(Unicode(255), nullable=False)
    display_name = Column(Unicode(64), nullable=False)
    employee_no = Column(Unicode(32))          # 對應門禁系統人員編號，如 EMP-081
    role = Column(Unicode(16), default="inspector")  # inspector/safety/manager/admin
    site_id = Column(Integer, ForeignKey("sites.id"))
    active = Column(Boolean, default=True)
    site = relationship("Site")


# --------------------------------------------------------------------------
# 表單模板（由 data/forms.json 匯入，來源為公司自主檢查表範本）
# --------------------------------------------------------------------------
class FormTemplate(Base):
    __tablename__ = "form_templates"
    form_code = Column(Unicode(8), primary_key=True)
    title = Column(Unicode(128), nullable=False)
    short_name = Column(Unicode(64))
    form_type = Column(Unicode(16))            # single=單次 / monthly=月曆型
    item_count = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    items = relationship("FormItem", back_populates="form",
                         order_by="FormItem.seq", cascade="all, delete-orphan")


class FormItem(Base):
    __tablename__ = "form_items"
    id = Column(Integer, primary_key=True)
    form_code = Column(Unicode(8), ForeignKey("form_templates.form_code"), nullable=False)
    seq = Column(Integer, nullable=False)
    category = Column(Unicode(64))
    hazard_code = Column(Unicode(16))
    hazard_label = Column(Unicode(32))
    text = Column(UnicodeText, nullable=False)
    form = relationship("FormTemplate", back_populates="items")
    __table_args__ = (UniqueConstraint("form_code", "seq", name="uq_form_seq"),)


# --------------------------------------------------------------------------
# 巡檢單
# --------------------------------------------------------------------------
class Inspection(Base):
    __tablename__ = "inspections"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    form_code = Column(Unicode(8), ForeignKey("form_templates.form_code"), nullable=False)
    inspect_date = Column(Date, nullable=False, default=date.today)
    location = Column(Unicode(128))            # 檢查地點 / 位置編號
    weather = Column(Unicode(16))
    inspector_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(Unicode(16), default="draft")   # draft/submitted/approved
    submitted_at = Column(DateTime)
    approved_at = Column(DateTime)
    pdf_path = Column(Unicode(255))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 「今日/近 N 日、某工地已交哪些巡檢單」是首頁與儀表板最常見的查詢
        Index("ix_inspections_date_site", "inspect_date", "site_id"),
    )

    site = relationship("Site")
    form = relationship("FormTemplate")
    inspector = relationship("User")
    results = relationship("InspectionResult", back_populates="inspection",
                           cascade="all, delete-orphan")
    signatures = relationship("Signature", back_populates="inspection",
                              cascade="all, delete-orphan")
    findings = relationship("Finding", back_populates="inspection")


class InspectionResult(Base):
    __tablename__ = "inspection_results"
    id = Column(Integer, primary_key=True)
    inspection_id = Column(Integer, ForeignKey("inspections.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("form_items.id"), nullable=False)
    day = Column(Integer)                     # 月曆型表單專用：1..31，單次型為 None
    result = Column(Unicode(8), nullable=False)  # pass/fail/na
    remark = Column(UnicodeText)
    inspection = relationship("Inspection", back_populates="results")
    item = relationship("FormItem")


# --------------------------------------------------------------------------
# 缺失單 —— 儀表板核心資料
# --------------------------------------------------------------------------
class Finding(Base):
    __tablename__ = "findings"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    inspection_id = Column(Integer, ForeignKey("inspections.id"))
    coordination_id = Column(Integer, ForeignKey("coordinations.id"))
    item_id = Column(Integer, ForeignKey("form_items.id"))
    source = Column(Unicode(24), default="inspection")
    # inspection=自主檢查 / coordination=協議巡視 / daily_report=應變小組日報 /
    # audit=主管抽查 / device=設備自動告警(未來由廠商 API 寫入)
    found_at = Column(DateTime, default=datetime.now)
    location = Column(Unicode(128))
    hazard_code = Column(Unicode(16))
    hazard_label = Column(Unicode(32))
    description = Column(UnicodeText, nullable=False)
    vendor_id = Column(Integer, ForeignKey("vendors.id"))
    responsible_person = Column(Unicode(64))
    severity = Column(Unicode(16), default="minor")     # minor/major/critical/stop_work
    action_type = Column(Unicode(16), default="onsite")  # onsite=當場改善 / scheduled=限期改善
    due_date = Column(Date)
    fixed_at = Column(DateTime)
    fix_note = Column(UnicodeText)
    verifier_id = Column(Integer, ForeignKey("users.id"))
    verified_at = Column(DateTime)
    status = Column(Unicode(16), default="open")   # open/fixed/verified/closed
    penalty = Column(Numeric(12, 2))
    photo_before = Column(Unicode(255))
    photo_after = Column(Unicode(255))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 儀表板主查詢：某區間（+某工地）的缺失
        Index("ix_findings_found_site", "found_at", "site_id"),
        # 未結案 / 逾期追蹤
        Index("ix_findings_status_due", "status", "due_date"),
        # 災害類別分布、廠商缺失排行
        Index("ix_findings_hazard", "hazard_code"),
        Index("ix_findings_vendor", "vendor_id"),
    )

    site = relationship("Site")
    vendor = relationship("Vendor")
    inspection = relationship("Inspection", back_populates="findings")
    coordination = relationship("Coordination", back_populates="findings")
    item = relationship("FormItem")

    @property
    def is_overdue(self) -> bool:
        if self.status in ("verified", "closed"):
            return False
        if self.action_type != "scheduled" or not self.due_date:
            return False
        return self.due_date < date.today()


# --------------------------------------------------------------------------
# 電子簽名
# --------------------------------------------------------------------------
class Signature(Base):
    __tablename__ = "signatures"
    id = Column(Integer, primary_key=True)
    inspection_id = Column(Integer, ForeignKey("inspections.id"))
    coordination_id = Column(Integer, ForeignKey("coordinations.id"))
    role = Column(Unicode(32), nullable=False)   # 工程專案主管/職安人員/主辦工程師/檢查人員
    signer_id = Column(Integer, ForeignKey("users.id"))
    signer_name = Column(Unicode(64), nullable=False)
    image_path = Column(Unicode(255), nullable=False)   # 手寫簽名 PNG
    signed_at = Column(DateTime, default=datetime.now)
    signed_ip = Column(Unicode(64))
    inspection = relationship("Inspection", back_populates="signatures")
    coordination = relationship("Coordination", back_populates="signatures")


# --------------------------------------------------------------------------
# 每日協議、巡視及處理紀錄表
# --------------------------------------------------------------------------
class Coordination(Base):
    __tablename__ = "coordinations"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    meeting_date = Column(Date, nullable=False, default=date.today)
    work_date = Column(Date, nullable=False, default=date.today)
    weather = Column(Unicode(16))
    agreement_text = Column(UnicodeText)      # 一、協議事項
    patrol_text = Column(UnicodeText)         # 二、巡視結果（缺失另立 Finding）
    handling_text = Column(UnicodeText)       # 三、處理情形
    status = Column(Unicode(16), default="draft")
    submitted_at = Column(DateTime)
    pdf_path = Column(Unicode(255))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.now)

    site = relationship("Site")
    attendees = relationship("CoordinationAttendee", back_populates="coordination",
                             cascade="all, delete-orphan")
    signatures = relationship("Signature", back_populates="coordination",
                              cascade="all, delete-orphan")
    findings = relationship("Finding", back_populates="coordination")


class CoordinationAttendee(Base):
    """參加協議人員。未來可由門禁 API 當日進場名單自動帶入。"""
    __tablename__ = "coordination_attendees"
    id = Column(Integer, primary_key=True)
    coordination_id = Column(Integer, ForeignKey("coordinations.id"), nullable=False)
    work_item = Column(Unicode(128))     # 作業項目
    vendor_id = Column(Integer, ForeignKey("vendors.id"))
    vendor_name = Column(Unicode(128))   # 若廠商不在主檔，允許自由填寫
    trade = Column(Unicode(64))          # 職種
    person_name = Column(Unicode(64))
    employee_no = Column(Unicode(32))
    work_content = Column(UnicodeText)
    coordination = relationship("Coordination", back_populates="attendees")


# --------------------------------------------------------------------------
# 設備資料落地表 —— 未來各設備廠商 API 推送進來的資料一律先進這裡
# --------------------------------------------------------------------------
class DeviceReading(Base):
    __tablename__ = "device_readings"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"))
    site_code = Column(Unicode(32))
    vendor_code = Column(Unicode(32))     # 設備商甲/設備商乙/設備商丙…
    device_type = Column(Unicode(32))     # access=門禁 / env=環境監測 / cctv_ai=影像辨識
    device_id = Column(Unicode(64))
    metric = Column(Unicode(64))          # headcount_in / pm25 / noise / alarm …
    value_num = Column(Numeric(18, 4))
    value_text = Column(Unicode(255))
    reading_at = Column(DateTime, nullable=False)
    raw_payload = Column(UnicodeText)
    received_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 設備資料是高頻寫入、依「工地＋設備類型＋時間」查詢，資料量會遠大於其他表
        Index("ix_device_site_type_time", "site_code", "device_type", "reading_at"),
    )


def init_db():
    Base.metadata.create_all(engine)


def db_info() -> str:
    """回傳目前連線的資料庫描述，供啟動訊息與健康檢查使用。"""
    if IS_SQLITE:
        return "SQLite（開發用）"
    if IS_MSSQL:
        if os.environ.get("DATABASE_URL"):
            return "SQL Server（由 DATABASE_URL 指定）"
        return f"SQL Server {MSSQL_SERVER} / {MSSQL_DATABASE}"
    return DATABASE_URL.split("://", 1)[0] + "（由 DATABASE_URL 指定）"
