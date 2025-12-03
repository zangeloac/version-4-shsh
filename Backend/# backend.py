# backend.py
import asyncio
import enum
import json
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sqlalchemy
from databases import Database

# --- CONFIGURATION ---
DATABASE_URL = "sqlite:///./smart_home.db"

# --- DB SETUP ---
metadata = sqlalchemy.MetaData()

devices = sqlalchemy.Table(
    "devices",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("type", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("state", sqlalchemy.String, nullable=False), # JSON string
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

sensors = sqlalchemy.Table(
    "sensors",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("type", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("is_triggered", sqlalchemy.Integer, nullable=False, default=0),
    sqlalchemy.Column("sensitivity", sqlalchemy.Float, nullable=False, default=1.0),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.utcnow),
)

events = sqlalchemy.Table(
    "events",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("timestamp", sqlalchemy.DateTime, nullable=False),
    sqlalchemy.Column("level", sqlalchemy.String, nullable=False), # info, warn, critical
    sqlalchemy.Column("source", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("payload", sqlalchemy.String, nullable=True),
)

engine = sqlalchemy.create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
metadata.create_all(engine)
db = Database(DATABASE_URL)

# --- MODELS ---
class DeviceType(str, enum.Enum):
    alarm = "alarm"
    light = "light"
    camera = "camera"
    lock = "lock"
    other = "other"

class DeviceIn(BaseModel):
    name: str
    type: str
    state: Optional[Dict[str, Any]] = Field(default_factory=dict)

class DeviceOut(DeviceIn):
    id: str
    created_at: datetime
    state: Dict[str, Any]

class SensorOut(BaseModel):
    id: str
    name: str
    type: str
    sensitivity: float
    is_triggered: bool
    created_at: datetime

class EventOut(BaseModel):
    id: str
    timestamp: datetime
    level: str
    source: str
    payload: Optional[Dict[str, Any]] = None

# --- APP ---
app = FastAPI(title="Smart Home Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active: self.active.remove(websocket)
    async def broadcast(self, message: dict):
        for conn in list(self.active):
            try: await conn.send_text(json.dumps(message, default=str))
            except: self.disconnect(conn)

manager = ConnectionManager()

# --- HELPERS ---
async def log_event(level: str, source: str, payload: Optional[dict] = None):
    ev_id = str(uuid.uuid4())
    now = datetime.utcnow()
    await db.execute(events.insert().values(
        id=ev_id, timestamp=now, level=level, source=source, 
        payload=json.dumps(payload) if payload else None
    ))
    evt = {"id": ev_id, "timestamp": now.isoformat(), "level": level, "source": source, "payload": payload}
    await manager.broadcast({"type": "event", "event": evt})
    return evt

# --- LIFECYCLE ---
@app.on_event("startup")
async def startup():
    await db.connect()
    # Seed default sensors if empty
    if await db.fetch_val(sqlalchemy.select([sqlalchemy.func.count()]).select_from(sensors)) == 0:
        now = datetime.utcnow()
        defaults = [
            ("Front Door", "door"), ("Living Room Motion", "motion"), 
            ("Kitchen Window", "window"), ("Smoke Detector", "alarm")
        ]
        for name, type_ in defaults:
            await db.execute(sensors.insert().values(
                id=str(uuid.uuid4()), name=name, type=type_, is_triggered=0, created_at=now
            ))

@app.on_event("shutdown")
async def shutdown():
    await db.disconnect()

# --- ROUTES ---
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/sensors", response_model=List[SensorOut])
async def list_sensors():
    rows = await db.fetch_all(sqlalchemy.select([sensors]))
    return [
        SensorOut(id=r["id"], name=r["name"], type=r["type"], sensitivity=r["sensitivity"], 
                  is_triggered=bool(r["is_triggered"]), created_at=r["created_at"]) 
        for r in rows
    ]

@app.post("/sensors/{sensor_id}/trigger")
async def trigger_sensor(sensor_id: str, body: dict):
    val = bool(body.get("value", True))
    await db.execute(sensors.update().where(sensors.c.id == sensor_id).values(is_triggered=int(val)))
    await log_event("warn" if val else "info", "Sensor Update", {"sensor_id": sensor_id, "triggered": val})
    return {"ok": True}

@app.get("/devices", response_model=List[DeviceOut])
async def list_devices():
    rows = await db.fetch_all(sqlalchemy.select([devices]))
    return [
        DeviceOut(id=r["id"], name=r["name"], type=r["type"], 
                  state=json.loads(r["state"]) if r["state"] else {}, created_at=r["created_at"]) 
        for r in rows
    ]

@app.post("/devices")
async def create_device(d: DeviceIn):
    dev_id = str(uuid.uuid4())
    await db.execute(devices.insert().values(
        id=dev_id, name=d.name, type=d.type, state=json.dumps(d.state), created_at=datetime.utcnow()
    ))
    return {"id": dev_id}

@app.patch("/devices/{device_id}")
async def update_device(device_id: str, payload: dict):
    # Fetch existing
    row = await db.fetch_one(sqlalchemy.select([devices]).where(devices.c.id == device_id))
    if not row: raise HTTPException(404, "Device not found")
    
    current_state = json.loads(row["state"] or "{}")
    current_state.update(payload)
    
    await db.execute(devices.update().where(devices.c.id == device_id).values(state=json.dumps(current_state)))
    await log_event("info", f"Device Update: {row['name']}", current_state)
    return {"ok": True}

@app.get("/events", response_model=List[EventOut])
async def get_events(limit: int = 50):
    q = sqlalchemy.select([events]).order_by(events.c.timestamp.desc()).limit(limit)
    rows = await db.fetch_all(q)
    return [
        EventOut(id=r["id"], timestamp=r["timestamp"], level=r["level"], source=r["source"], 
                 payload=json.loads(r["payload"]) if r["payload"] else None) 
        for r in rows
    ]

@app.post("/logs")
async def create_manual_log(body: dict):
    # Allow frontend to push manual simulator logs
    await log_event(body.get("level", "info"), body.get("source", "Manual"), body.get("payload"))
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)