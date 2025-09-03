"""
おくすりリマインダー（LINE + FastAPI）ベース実装
--------------------------------------------------
最小構成：
- DB: SQLite（本番は MySQL。DATABASE_URL で切替）
- API: FastAPI
- 通知: LINE Messaging API（push）
- ジョブ: APScheduler（毎分チェック）
- 機能: ユーザー登録（自動/フォロー時）、薬&スケジュール登録（REST）、
        リマインド送信、"飲んだ" / "あとで"（スヌーズ）記録、当日履歴確認

必要パッケージ（requirements）:
fastapi==0.115.0
uvicorn[standard]==0.30.6
SQLAlchemy==2.0.36
pydantic==2.9.2
python-dotenv==1.0.1
APScheduler==3.10.4
line-bot-sdk==3.13.0
pytz==2024.1

.env 例：
LINE_CHANNEL_SECRET=xxxxxxxxxxxxxxxx
LINE_CHANNEL_ACCESS_TOKEN=xxxxxxxxxxxxxxxx
DATABASE_URL=sqlite:///./reminder.db
TZ=Asia/Tokyo
SNOOZE_MINUTES=15

起動：
$ uvicorn okusu-reminder-fastapi-base:app --reload --port 8000

動作確認：
1) RESTでテスト用データ投入（本番はLINE対話での登録を拡張）
   POST /admin/users -> ユーザー作成（line_user_id を仮で指定）
   POST /admin/medicines -> 薬 + スケジュール作成
2) スケジューラが毎分チェックし、該当時刻に push 通知
3) LINEのボタンから「飲んだ」「あとで（スヌーズ）」が反映
4) LINEで「履歴」と送ると当日・前日の履歴要約

⚠️ 本番運用メモ：
- Webhookの検証/署名チェックは有効（X-Line-Signature）
- HTTPS 必須。さくらのリバースプロキシ or Cloudflare を利用
- MySQL へ移行時は DATABASE_URL を mysql+pymysql:// に変更
- テーブルは Alembic 移行推奨（ここでは Base.metadata.create_all）
"""
from __future__ import annotations

import os
import hmac
import hashlib
from datetime import datetime, date, time, timedelta
from typing import Optional, List

import pytz
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, Integer, String, Date, DateTime, Time, Boolean,
    ForeignKey, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

# LINE SDK
from linebot import LineBotApi, WebhookParser
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent, PostbackEvent,
    TemplateSendMessage, ButtonsTemplate, PostbackAction
)

load_dotenv()

# === 設定 ===
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./reminder.db")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
TZ_NAME = os.getenv("TZ", "Asia/Tokyo")
SNOOZE_MINUTES = int(os.getenv("SNOOZE_MINUTES", "15"))

TZ = pytz.timezone(TZ_NAME)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

line_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
parser = WebhookParser(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# === モデル ===
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    line_user_id = Column(String(64), unique=True, index=True, nullable=False)
    display_name = Column(String(128), nullable=True)
    medicines = relationship("Medicine", back_populates="user", cascade="all, delete-orphan")

class Medicine(Base):
    __tablename__ = "medicines"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    instructions = Column(String(128), nullable=True)  # 例: "1日2回"
    note = Column(Text, nullable=True)
    schedules = relationship("Schedule", back_populates="medicine", cascade="all, delete-orphan")
    user = relationship("User", back_populates="medicines")

class Schedule(Base):
    __tablename__ = "schedules"
    id = Column(Integer, primary_key=True)
    medicine_id = Column(Integer, ForeignKey("medicines.id"), nullable=False, index=True)
    time_of_day = Column(Time, nullable=False)  # 毎日この時刻
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    method = Column(String(32), nullable=True)  # 食前/食後など
    active = Column(Boolean, nullable=False, default=True)
    last_notified_at = Column(DateTime, nullable=True)
    snooze_until = Column(DateTime, nullable=True)

    medicine = relationship("Medicine", back_populates="schedules")
    intake_logs = relationship("IntakeLog", back_populates="schedule", cascade="all, delete-orphan")

class IntakeLog(Base):
    __tablename__ = "intake_logs"
    id = Column(Integer, primary_key=True)
    schedule_id = Column(Integer, ForeignKey("schedules.id"), nullable=False, index=True)
    taken_at = Column(DateTime, nullable=False)
    status = Column(String(16), nullable=False)  # taken/snoozed/missed

    schedule = relationship("Schedule", back_populates="intake_logs")

Base.metadata.create_all(engine)

# === Pydantic Schemas（REST 用） ===
class CreateUserIn(BaseModel):
    line_user_id: str = Field(..., examples=["Uxxxxxxxxxx"])
    display_name: Optional[str] = None

class CreateScheduleIn(BaseModel):
    time_of_day: str = Field(..., examples=["08:00"])  # HH:MM
    start_date: date
    end_date: Optional[date] = None
    method: Optional[str] = None

class CreateMedicineIn(BaseModel):
    line_user_id: str
    name: str
    instructions: Optional[str] = None
    note: Optional[str] = None
    schedules: List[CreateScheduleIn]

# === Utility ===

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def now_tz() -> datetime:
    return datetime.now(TZ)


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def within_active_window(sch: Schedule, today: date) -> bool:
    if not sch.active:
        return False
    if sch.start_date and today < sch.start_date:
        return False
    if sch.end_date and today > sch.end_date:
        return False
    return True


# === 通知関連 ===

def push_reminder(schedule: Schedule):
    if not line_api:
        print("[WARN] LINE credentials not set. Skipping push.")
        return
    user = schedule.medicine.user
    med = schedule.medicine

    body = f"{med.name} の時間です。({schedule.time_of_day.strftime('%H:%M')})\n飲みましたか？"

    actions = [
        PostbackAction(label="飲んだ", data=f"taken:{schedule.id}"),
        PostbackAction(label=f"あとで({SNOOZE_MINUTES}分)", data=f"snooze:{schedule.id}"),
    ]

    tmpl = TemplateSendMessage(
        alt_text=body,
        template=ButtonsTemplate(title="おくすりリマインダー", text=body, actions=actions),
    )
    try:
        line_api.push_message(user.line_user_id, tmpl)
        print(f"[PUSH] sent to {user.line_user_id} schedule_id={schedule.id}")
    except Exception as e:
        print("[ERROR] push failed:", e)


# === スケジューラ ===

def scan_and_notify():
    """毎分起動。該当時刻のスケジュールに通知を送る。"""
    db = SessionLocal()
    try:
        now = now_tz()
        today = now.date()
        current_hhmm = now.strftime('%H:%M')

        # snooze_until が未来なら飛ばす
        qs = (
            db.query(Schedule)
            .join(Medicine)
            .join(User)
            .filter(Schedule.active == True)
        )
        for sch in qs:
            if not within_active_window(sch, today):
                continue
            # スヌーズ中
            if sch.snooze_until and now < sch.snooze_until:
                continue
            # 今日の指定時刻に一致？（分まで合わせる）
            if sch.time_of_day.strftime('%H:%M') != current_hhmm:
                # 同分以外はスキップ
                continue
            # 同分に既に通知済みか（last_notified_at が同分ならスキップ）
            if sch.last_notified_at and sch.last_notified_at.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                continue
            # すでに taken があるなら通知不要
            taken_exists = (
                db.query(IntakeLog)
                .filter(IntakeLog.schedule_id == sch.id)
                .filter(IntakeLog.status == 'taken')
                .filter(IntakeLog.taken_at >= datetime.combine(today, time(0,0), tzinfo=TZ))
                .first()
            )
            if taken_exists:
                continue

            # 通知実行
            push_reminder(sch)
            sch.last_notified_at = now
            sch.snooze_until = None
            db.add(sch)
        db.commit()
    except Exception as e:
        print("[ERROR] scan_and_notify:", e)
    finally:
        db.close()


scheduler = BackgroundScheduler(timezone=TZ_NAME)
scheduler.add_job(scan_and_notify, IntervalTrigger(minutes=1))
scheduler.start()

# === FastAPI アプリ ===
app = FastAPI(title="Okusu Reminder API")


@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown(wait=False)


# 依存関数

def get_or_create_user(db: Session, line_user_id: str, display_name: Optional[str] = None) -> User:
    u = db.query(User).filter_by(line_user_id=line_user_id).first()
    if u:
        return u
    u = User(line_user_id=line_user_id, display_name=display_name)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# === 管理用REST（開発時の投入・確認） ===
@app.post("/admin/users")
def create_user(payload: CreateUserIn, db: Session = Depends(get_db)):
    u = get_or_create_user(db, payload.line_user_id, payload.display_name)
    return {"id": u.id, "line_user_id": u.line_user_id}


@app.post("/admin/medicines")
def create_medicine(payload: CreateMedicineIn, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(line_user_id=payload.line_user_id).first()
    if not user:
        raise HTTPException(404, "user not found")
    med = Medicine(user_id=user.id, name=payload.name, instructions=payload.instructions, note=payload.note)
    db.add(med)
    db.flush()
    for sch_in in payload.schedules:
        sch = Schedule(
            medicine_id=med.id,
            time_of_day=parse_hhmm(sch_in.time_of_day),
            start_date=sch_in.start_date,
            end_date=sch_in.end_date,
            method=sch_in.method,
            active=True,
        )
        db.add(sch)
    db.commit()
    db.refresh(med)
    return {"medicine_id": med.id, "schedules": [s.id for s in med.schedules]}


@app.get("/health")
def health():
    return {"ok": True, "time": now_tz().isoformat()}


# === LINE Webhook ===
@app.post("/callback")
async def callback(request: Request, db: Session = Depends(get_db)):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    print("Webhook受信:", body)

    if not parser:
        raise HTTPException(500, "LINE parser not configured")
    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        # テキストメッセージ受信時に返信
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):
            if line_api:
                line_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"受け取りました: {event.message.text}")
                )
        # ...既存のフォロー・履歴・postback処理...
        if isinstance(event, FollowEvent):
            uid = event.source.user_id
            profile = None
            try:
                if line_api:
                    profile = line_api.get_profile(uid)
            except Exception:
                profile = None
            get_or_create_user(db, uid, getattr(profile, 'display_name', None))
            if line_api:
                from linebot.models import QuickReply, QuickReplyButton, MessageAction
                line_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="はじめまして！お薬リマインダーです。\nまずは登録を始めましょう。",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=MessageAction(label="薬を登録する", text="薬を登録")),
                            QuickReplyButton(action=MessageAction(label="使い方を見る", text="使い方"))
                        ])
                    )
                )

        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessage):
            txt = (event.message.text or "").strip()
            if txt in ("履歴", "りれき", "history"):
                uid = event.source.user_id
                user = db.query(User).filter_by(line_user_id=uid).first()
                if not user:
                    if line_api:
                        line_api.reply_message(event.reply_token, TextSendMessage(text="ユーザー未登録です。いったんブロック→再フォローで登録してください。"))
                else:
                    msg = summarize_history(db, user)
                    if line_api:
                        line_api.reply_message(event.reply_token, TextSendMessage(text=msg))

        if isinstance(event, PostbackEvent):
            data = event.postback.data or ""
            if data.startswith("taken:"):
                schedule_id = int(data.split(":")[1])
                mark_taken(db, schedule_id)
                if line_api:
                    line_api.reply_message(event.reply_token, TextSendMessage(text="記録しました。おつかれさま！"))
            elif data.startswith("snooze:"):
                schedule_id = int(data.split(":")[1])
                set_snooze(db, schedule_id)
                if line_api:
                    line_api.reply_message(event.reply_token, TextSendMessage(text=f"{SNOOZE_MINUTES}分後に再通知します。"))

    return JSONResponse({"status": "ok"})


# === ドメインロジック ===

def summarize_history(db: Session, user: User) -> str:
    now = now_tz()
    today = now.date()
    yesterday = today - timedelta(days=1)

    def day_summary(d: date) -> str:
        logs = (
            db.query(IntakeLog)
            .join(Schedule)
            .join(Medicine)
            .filter(Medicine.user_id == user.id)
            .filter(IntakeLog.taken_at >= datetime.combine(d, time(0,0), tzinfo=TZ))
            .filter(IntakeLog.taken_at < datetime.combine(d + timedelta(days=1), time(0,0), tzinfo=TZ))
            .all()
        )
        if not logs:
            return f"{d.strftime('%-m/%-d')}：記録なし"
        counts = {"taken": 0, "snoozed": 0, "missed": 0}
        for l in logs:
            counts[l.status] = counts.get(l.status, 0) + 1
        return f"{d.strftime('%-m/%-d')}：飲んだ {counts['taken']} 件 / スヌーズ {counts['snoozed']} 件"

    return "\n".join(["本日の履歴", day_summary(today), "", "昨日の履歴", day_summary(yesterday)])


def mark_taken(db: Session, schedule_id: int):
    now = now_tz()
    sch = db.get(Schedule, schedule_id)
    if not sch:
        return
    log = IntakeLog(schedule_id=sch.id, taken_at=now, status='taken')
    sch.snooze_until = None
    db.add_all([log, sch])
    db.commit()


def set_snooze(db: Session, schedule_id: int):
    now = now_tz()
    sch = db.get(Schedule, schedule_id)
    if not sch:
        return
    sch.snooze_until = now + timedelta(minutes=SNOOZE_MINUTES)
    log = IntakeLog(schedule_id=sch.id, taken_at=now, status='snoozed')
    db.add_all([log, sch])
    db.commit()


# === 参考: シンプルな登録UIをLINEに拡張する時の方針 ===
"""
- 現状は REST で薬・スケジュールを投入。次の拡張で LINE 対話登録を追加：
  1) ユーザーが「登録」と送信 → ステップ1: 薬名、2: 時刻、3: 開始日、4: 日数 の順に質問
  2) 入力途中の状態は pending_flows テーブル（user_id, step, payload_json）に保存
  3) 最終確認（はい/いいえ） → 確定で DB 登録
- 週次サマリーは APScheduler で毎週○曜○時に push するジョブを追加
"""
