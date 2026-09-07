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
    # timeout：工地檢視器（site_runner）是三條收集執行緒＋API 同進程共用
    # 這一個檔，「更新工項.cmd」還會另開一個程序寫入。預設 5 秒的鎖等待
    # 在首輪全量同步的長交易期間不夠，撞上就是 database is locked、
    # 該輪資料靜默消失。
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
elif IS_MSSQL:
    # pyodbc 批次寫入加速，僅 mssql+pyodbc 支援，其他方言傳入會直接報錯
    _engine_kwargs["fast_executemany"] = True

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if IS_SQLITE:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_concurrency(dbapi_conn, _record):
        # WAL 讓「讀」不被「寫」擋住：牆面每分鐘查詢、收集程式隨時寫入，
        # 預設 rollback journal 之下兩者互斥
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
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
    # 事業處。雲端 migration 004 就加了，地端一直沒跟上，而共用前端的工地
    # 下拉是依 department 分組的（frontend/common.js），少了它就分不了組。
    department = Column(Unicode(64))
    sort_order = Column(Integer, nullable=False, default=0)
    board_config = Column(UnicodeText)
    board_revision = Column(Integer, default=0)


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
# 表單模板（由 backend/data/forms.json 匯入，來源為公司自主檢查表範本）
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
    # 雲端那一筆的 id。表單留在雲端讓工地用手機填報，戰情室在地端，
    # 由 collectors/sync_forms.py 定時抓回來。比對用這個欄位而不是 id——
    # 兩邊各自產生流水號，直接沿用會撞號，把不同的資料蓋掉。
    cloud_id = Column(Integer, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    # 棟別（如辦公棟／住宅棟）。主場站領有兩張建照但實為同一塊工地，
    # 填報不拆成兩個工地，以棟別區分。允許為空：既有資料沒有這個欄位。
    building = Column(Unicode(32))
    form_code = Column(Unicode(8), ForeignKey("form_templates.form_code"), nullable=False)
    inspect_date = Column(Date, nullable=False, default=date.today)
    location = Column(Unicode(128))            # 檢查地點 / 位置編號
    weather = Column(Unicode(16))
    inspector_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # 實際執行檢查的人。現場共用同一組帳號登入，inspector_id 只能代表
    # 「哪個工地帳號送的」，答不出「這張表是誰檢查的」，而稽核與事故調查
    # 都要問這件事。允許為空：既有資料沒有這個欄位，補值只會是猜測。
    inspector_name = Column(Unicode(64))
    status = Column(Unicode(16), default="draft")   # draft/submitted/approved
    submitted_at = Column(DateTime)
    approved_at = Column(DateTime)
    pdf_path = Column(Unicode(255))
    created_at = Column(DateTime, default=datetime.now)
    # 由雲端同步過來的不符合項數。同步只帶彙總、不帶逐項結果——逐項資料量大，
    # 而牆上只需要「幾項不符合」。本機自己填的表單此欄為空，改由 results 計算。
    fail_count = Column(Integer)

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
    # 雲端那一筆的 id。表單留在雲端讓工地用手機填報，戰情室在地端，
    # 由 collectors/sync_forms.py 定時抓回來。比對用這個欄位而不是 id——
    # 兩邊各自產生流水號，直接沿用會撞號，把不同的資料蓋掉。
    cloud_id = Column(Integer, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    # 棟別，沿用所屬表單（見 Inspection.building 的說明）
    building = Column(Unicode(32))
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
    # 雲端那一筆的 id。表單留在雲端讓工地用手機填報，戰情室在地端，
    # 由 collectors/sync_forms.py 定時抓回來。比對用這個欄位而不是 id——
    # 兩邊各自產生流水號，直接沿用會撞號，把不同的資料蓋掉。
    cloud_id = Column(Integer, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    # 棟別（見 Inspection.building 的說明）
    building = Column(Unicode(32))
    meeting_date = Column(Date, nullable=False, default=date.today)
    work_date = Column(Date, nullable=False, default=date.today)
    weather = Column(Unicode(16))
    # 同 Inspection.inspector_name，協議表記錄實際填表人
    recorder_name = Column(Unicode(64))
    agreement_text = Column(UnicodeText)      # 一、協議事項
    patrol_text = Column(UnicodeText)         # 二、巡視結果（缺失另立 Finding）
    handling_text = Column(UnicodeText)       # 三、處理情形
    status = Column(Unicode(16), default="draft")
    submitted_at = Column(DateTime)
    pdf_path = Column(Unicode(255))
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.now)
    # 同上：由雲端同步過來的出席廠商家數
    attendee_count = Column(Integer)

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


class WorkLog(Base):
    """本日出工回報——工地看板「本日出工一覽表」的資料來源。

    來源是工務所 LINE 群組的出工回報訊息（經 webhook 落到 Google 試算表，
    collectors/worklog.py 定時抓 CSV 解析）。訊息是自由文字、每家廠商
    寫法不同，解析採啟發式，因此保留 raw 原文供對照與除錯。

    同一天同一棟同一廠商常會重發更正版，以 (report_date, building, vendor)
    為鍵、取最新一則覆蓋——收集程式據此 upsert，不設資料庫唯一鍵：
    解析歧義造成的重複寧可看得到，也不要讓整批寫入炸掉。
    """
    __tablename__ = "work_logs"
    id = Column(Integer, primary_key=True)
    report_date = Column(Date, nullable=False)
    building = Column(Unicode(32))         # 棟別（與填報的棟別同一詞彙）
    vendor = Column(Unicode(64), nullable=False)
    trade = Column(Unicode(128))           # 工種與人數摘要，如「電班9、工務2」
    headcount = Column(Integer)            # 出工人數（出工數優先，否則加總）
    supervisor = Column(Unicode(64))       # 作業主管——訊息有寫才有值
    tasks = Column(UnicodeText)            # 施作項目
    reporter = Column(Unicode(64))         # LINE 回報人
    reported_at = Column(DateTime)         # LINE 訊息時間
    message_id = Column(Unicode(64))       # LINE 訊息 ID，除錯對照用
    raw = Column(UnicodeText)
    fetched_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 看板每分鐘查「今日出工」，月工時統計依日期範圍掃
        Index("ix_worklog_date", "report_date"),
    )


class NewsItem(Base):
    """職安新知——看板「安全佈告／宣導」輪播的外部來源。

    collectors/osha_news.py 定時抓職安署網站的新聞稿清單。
    news_id 是對方網站的文章編號，upsert 的比對鍵。
    """
    __tablename__ = "news_items"
    id = Column(Integer, primary_key=True)
    source = Column(Unicode(16), nullable=False, default="osha")
    news_id = Column(Unicode(32), nullable=False)
    title = Column(Unicode(255), nullable=False)
    url = Column(Unicode(300))
    published = Column(Date)
    fetched_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("source", "news_id", name="uq_news_source_id"),
    )


class PlannedTask(Base):
    """排程工項——儀表板「今日重點工項」的資料來源。

    目前由 backend/tools/import_schedule.py 從各工地的列控表（MS Project 匯出的
    project 工作表）整份匯入，source='schedule'。未來現場改為每日回報
    實際進度時，以 source='daily_report' 寫入即可並存：儀表板可優先取
    日報、沒有日報的日子退回排程，不必改資料結構。

    匯入是整份替換（先刪同 site+source 再寫入）：列控表每月改版，
    逐筆比對新舊版本的工項沒有可靠的鍵——名稱會改、識別碼會重排。
    """
    __tablename__ = "planned_tasks"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("sites.id"))
    site_code = Column(Unicode(32), nullable=False)
    source = Column(Unicode(16), nullable=False, default="schedule")
    task_no = Column(Unicode(16))          # 列控表內的識別碼，僅供對照
    name = Column(Unicode(255), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    outline_level = Column(Integer)
    # 只有葉節點是實際的作業；上層是「基礎工程」這類彙總，掛上牆沒有意義
    is_leaf = Column(Boolean, nullable=False, default=True)
    imported_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        # 儀表板每次載入都查「今日進行中」，依日期範圍掃
        Index("ix_ptask_site_dates", "site_code", "start_date", "end_date"),
    )


def _add_missing_columns():
    """替既有資料表補上模型有、資料庫還沒有的欄位。

    create_all() 只建立缺少的「表」，不會替既有的表加欄位，因此舊的資料庫
    升級後會在寫入時直接炸掉。雲端那側有 netlify/database/migrations/，
    地端沒有遷移工具。

    欄位清單直接從模型推導，不另外手工維護一份——手工清單漏掉的欄位不會有
    任何人發現：migration 004 的 sites.department 就是這樣在地端缺了很久，
    而共用前端的工地下拉正是依它分組的。
    """
    from sqlalchemy import inspect as sa_inspect, text
    from sqlalchemy.schema import CreateColumn

    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue              # create_all 已建好，屆時就含全部欄位
        have = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in have]
        if not missing:
            continue

        # 逐欄各自提交：某一欄失敗（例如既有資料違反 NOT NULL）不該讓其他
        # 欄位跟著回滾，也不該讓整個服務起不來。
        for col in missing:
            ddl = CreateColumn(col).compile(engine).string
            # NOT NULL 欄位加到「已有資料」的表時，SQL Server 一定要有 DEFAULT，
            # 否則整句被拒（既有列沒值可填）。模型裡的 default= 是 Python 端預設，
            # 不會出現在 CreateColumn 產生的 DDL 裡，因此這裡自行補上伺服器端
            # 預設值。取不到純量預設值時，退而以可為空的方式加入——欄位存在
            # 但少了 NOT NULL 約束，總比整個欄位加不進去、之後寫入全炸好。
            if not col.nullable:
                lit = _scalar_default_sql(col)
                if lit is not None:
                    ddl += f" DEFAULT {lit}"
                else:
                    ddl = ddl.replace(" NOT NULL", "")
                    print(f"[db] {table.name}.{col.name} 無可用預設值，"
                          "改以可為空方式補上")
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD {ddl}"))
            except Exception as e:                    # noqa: BLE001
                print(f"[db] 無法補上 {table.name}.{col.name}：{e}")


def _scalar_default_sql(col):
    """把欄位的純量預設值轉成可直接放進 DDL 的字面量；取不到時回 None。

    只處理純量常數（default=0、default="draft" 這類）。像 datetime.now 這種
    可呼叫的預設是每列各自求值，沒有單一字面量可放進 ALTER，回 None 由呼叫端
    改以可為空方式處理。
    """
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return None
    val = d.arg
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return None


# 建表與補欄位時的鎖等待上限（毫秒）。
#
# 服務被強制結束時，SQL Server 那端的 session 不會立刻消失，未結束的交易
# 會繼續握著 schema 鎖。下次啟動查系統目錄就被擋住，而預設是**無限等待**——
# 症狀是服務停在「Waiting for application startup」永遠不動，沒有任何錯誤訊息。
# 工作排程器開機自動啟動時遇到這個，牆上就是一片空白而沒有人知道為什麼。
#
# 設上限讓它失敗得快、而且講得出原因。
DDL_LOCK_TIMEOUT_MS = 15000


_INIT_DONE = False


def init_db():
    # 同一進程只做一次。工地檢視器（site_runner）會先自己呼叫一次、
    # FastAPI 的 startup 事件又呼叫一次——建表與補欄位是啟動成本，
    # 不必付兩遍。
    global _INIT_DONE
    if _INIT_DONE:
        return
    _INIT_DONE = True
    if IS_MSSQL:
        from sqlalchemy import event, text as _text

        # 只掛在這一段，不影響服務啟動後的一般查詢——那些查詢慢是慢，
        # 但不該因為短暫的鎖競爭就整個失敗。
        def _set_lock_timeout(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute(f"SET LOCK_TIMEOUT {DDL_LOCK_TIMEOUT_MS}")
            cur.close()

        event.listen(engine, "connect", _set_lock_timeout)
        try:
            _create_schema()
        except Exception as e:                        # noqa: BLE001
            raise RuntimeError(
                f"資料庫初始化失敗或在等待鎖時逾時"
                f"（上限 {DDL_LOCK_TIMEOUT_MS} 毫秒）。"
                "常見原因是上一次服務被強制結束，留下未結束的交易握著結構鎖。"
                "處理方式：確認沒有其他行程正在使用這個資料庫，必要時在 "
                "SQL Server 以 KILL 結束殘留的 session 後再啟動。"
                f"原始錯誤：{e}") from e
        finally:
            event.remove(engine, "connect", _set_lock_timeout)
            engine.dispose()      # 收掉帶有 LOCK_TIMEOUT 的連線，不留給後續查詢
    else:
        _create_schema()


def _create_schema():
    Base.metadata.create_all(engine)
    _add_missing_columns()


def db_info() -> str:
    """回傳目前連線的資料庫描述，供啟動訊息與健康檢查使用。"""
    if IS_SQLITE:
        return "SQLite（開發用）"
    if IS_MSSQL:
        if os.environ.get("DATABASE_URL"):
            return "SQL Server（由 DATABASE_URL 指定）"
        return f"SQL Server {MSSQL_SERVER} / {MSSQL_DATABASE}"
    return DATABASE_URL.split("://", 1)[0] + "（由 DATABASE_URL 指定）"
