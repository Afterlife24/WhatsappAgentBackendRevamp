import os
import asyncio
import time
import hashlib
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from openai import AsyncOpenAI
from collections import deque
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import ConnectionFailure

# ---------------- CONFIG ----------------
MAX_HISTORY = 8  # Enough to keep KB + conversation in context
MODEL_NAME = "gpt-4o-mini"  # Cheaper and good quality
MAX_TOKENS = 150  # Enough for complete WhatsApp replies without cutoff
TIMEOUT = 10  # Slightly increased for longer responses
TEMPERATURE = 0.1  # Lower = faster, more deterministic
KB_CHECK_INTERVAL_HOURS = 24  # How often to check for KB updates per user

# ----------------------------------------

load_dotenv()

# --- LOAD ALL ENV VARS ONCE AT STARTUP ---


class Config:
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")
    MONGODB_URL: str = os.getenv("MONGODB_URL")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "WhatsappChat24hrs")
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_WHATSAPP_NUMBER: str = os.getenv("TWILIO_WHATSAPP_NUMBER")
    CONTENT_SID: str = os.getenv("CONTENT_SID")
    FORM_SENDING_SID: str = os.getenv("FORM_SENDING_SID")
    HUMAN_TAKEOVER_SID: str = os.getenv("HUMAN_TAKEOVER_SID")
    OWNER_ALERT_SID: str = os.getenv("OWNER_ALERT_SID")
    OWNER_WHATSAPP_NUMBER: str = os.getenv("OWNER_WHATSAPP_NUMBER")


config = Config()

client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)

# --- MONGODB CONNECTION ---
MONGODB_URL = config.MONGODB_URL
MONGODB_DATABASE = config.MONGODB_DATABASE

mongo_client = None
db = None
sessions_collection = None
chats_collection = None

try:
    mongo_client = AsyncIOMotorClient(
        MONGODB_URL, serverSelectionTimeoutMS=5000)
    db = mongo_client[MONGODB_DATABASE]
    sessions_collection = db['sessions']
    chats_collection = db['chats']
    print(f"✅ Connected to MongoDB: {MONGODB_DATABASE}")
except Exception as e:
    print(f"❌ MongoDB setup error: {e}")
    print("⚠️ Falling back to JSON file storage")

# --- PROFESSIONAL CREDENTIALS ---
TWILIO_ACCOUNT_SID = config.TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN = config.TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_NUMBER = config.TWILIO_WHATSAPP_NUMBER
CONTENT_SID = config.CONTENT_SID
FORM_SENDING_SID = config.FORM_SENDING_SID
HUMAN_TAKEOVER_SID = config.HUMAN_TAKEOVER_SID
OWNER_ALERT_SID = config.OWNER_ALERT_SID
OWNER_WHATSAPP_NUMBER = config.OWNER_WHATSAPP_NUMBER

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        print(f"Warning: Could not initialize Twilio client: {e}")

app = FastAPI()

# --- CREATE INDEXES ON STARTUP ---


@app.on_event("startup")
async def create_indexes():
    if mongo_client is None or sessions_collection is None:
        print("⚠️ Skipping index creation, MongoDB not connected")
        return
    try:
        # Drop old TTL index if it exists so we can replace it with a plain index
        try:
            await sessions_collection.drop_index("last_activity_1")
            print("🗑️ Dropped old TTL index on last_activity")
        except Exception:
            pass  # Index didn't exist, nothing to drop

        await sessions_collection.create_index("last_activity")
        await chats_collection.create_index(
            [("phone_number", ASCENDING), ("timestamp", ASCENDING)]
        )
        await mongo_client.admin.command('ping')
        print("✅ MongoDB indexes created and connection verified")
    except Exception as e:
        print(f"❌ MongoDB index creation failed: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- LOAD PROMPTS --------
with open("system_prompt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open("knowledge_base.txt", "r", encoding="utf-8") as f:
    KNOWLEDGE_BASE = f.read()


def get_kb_hash() -> str:
    """Return MD5 hash of the current knowledge_base.txt content."""
    return hashlib.md5(KNOWLEDGE_BASE.encode("utf-8")).hexdigest()


def reload_knowledge_base() -> str:
    """Re-read knowledge_base.txt from disk and return fresh content."""
    global KNOWLEDGE_BASE
    with open("knowledge_base.txt", "r", encoding="utf-8") as f:
        KNOWLEDGE_BASE = f.read()
    return KNOWLEDGE_BASE


# Greeting sent once on first message only
GREETING_MESSAGE = "Hi! Welcome to Autonomiq AI.\n\nWhat brings you here today?"

# -------- MEMORY PER USER --------
USER_HISTORY = {}

# -------- HUMAN TAKEOVER STATE --------
HUMAN_TAKEOVER = {}  # {phone_number: True/False}
PENDING_HUMAN_CONFIRMATION = {}  # {phone_number: True/False}

# -------- KB INJECTION STATE (Fallback for non-MongoDB) --------
KB_INJECTED = {}  # {phone_number: True/False}

# -------- MESSAGE STORAGE (Fallback for non-MongoDB) --------
MESSAGE_STORE = {}  # {phone_number: [{sender, content, timestamp, type}]}
LAST_USER_MESSAGE_TIME = {}  # Track when user last messaged
STORAGE_FILE = Path("conversation_data.json")

# -------- SESSION CACHE (5-minute TTL, reduces MongoDB reads) --------
# {phone_number: {"data": dict, "expires_at": float}}
SESSION_CACHE: dict = {}
SESSION_CACHE_TTL = 300           # 5 minutes in seconds

# -------- DASHBOARD CACHE (30-second TTL) --------
CONVERSATIONS_CACHE: dict = {"data": None, "expires_at": 0.0}

# -------- INFLIGHT REQUESTS (OpenAI deduplication) --------
INFLIGHT_REQUESTS: dict = {}      # {cache_key: asyncio.Future}

# -------- FAQ RESPONSE CACHE (1-hour TTL) --------
# {normalized_message: {"reply": str, "expires_at": float}}
OPENAI_RESPONSE_CACHE: dict = {}
FAQ_CACHE_TTL = 3600              # 1 hour in seconds

# Keywords that indicate an FAQ-style cacheable question
FAQ_KEYWORDS = {
    "service", "services", "product", "products", "provide",
    "autonomiq", "company", "about", "detail", "details",
    "price", "pricing", "cost", "costs", "plan", "plans",
    "feature", "features", "offer", "offering", "solution", "solutions",
    "demo", "contact", "support",
    "agent", "agents", "bot", "bots", "automation", "ai"
}

# Exact short phrases that are context-dependent — never cache
NON_CACHEABLE_EXACT = {
    "yes", "no", "ok", "okay", "sure",
    "nope", "yep", "yeah",
    "exactly", "correct", "right",
    "alright", "got it",
    "tell me more", "more info",
    "go on", "continue", "next",
    "thanks", "thank you",
    "ok thanks", "okay thanks",
    "i see", "interesting",
    "what else", "and then",
    "anything else",
    "explain more",
    "can you explain"
}


def is_faq_cacheable(message: str) -> bool:
    normalized = " ".join(
        message.lower().strip().split()
    )

    if len(normalized) < 5:
        return False

    if normalized in NON_CACHEABLE_EXACT:
        return False

    faq_patterns = {
        "services": [
            "what services",
            "your services",
            "what do you do",
            "what can you do",
            "offerings"
        ],
        "pricing": [
            "pricing",
            "price",
            "cost",
            "charges",
            "plans"
        ],
        "company": [
            "about autonomiq",
            "about company",
            "company details",
            "who are you"
        ],
        "product": [
            "product",
            "products",
            "features",
            "demo",
            "automation"
        ]
    }

    for patterns in faq_patterns.values():
        if any(p in normalized for p in patterns):
            return True

    return False


def faq_cache_get(normalized_key: str) -> str | None:
    """Return cached reply if valid, else None."""
    entry = OPENAI_RESPONSE_CACHE.get(normalized_key)
    if entry and time.monotonic() < entry["expires_at"]:
        return entry["reply"]
    OPENAI_RESPONSE_CACHE.pop(normalized_key, None)
    return None


def faq_cache_set(normalized_key: str, reply: str):
    """Store reply in FAQ cache with TTL."""
    OPENAI_RESPONSE_CACHE[normalized_key] = {
        "reply": reply,
        "expires_at": time.monotonic() + FAQ_CACHE_TTL
    }

# -------- SESSION CACHE HELPERS --------


def _cache_get(phone_number: str) -> dict | None:
    """Return cached session if still valid, else None."""
    entry = SESSION_CACHE.get(phone_number)
    if entry and time.monotonic() < entry["expires_at"]:
        return entry["data"]
    # Expired — evict
    SESSION_CACHE.pop(phone_number, None)
    return None


def _cache_set(phone_number: str, session: dict):
    """Store session in cache with TTL."""
    SESSION_CACHE[phone_number] = {
        "data": session,
        "expires_at": time.monotonic() + SESSION_CACHE_TTL
    }


def _cache_update(phone_number: str, fields: dict):
    """Merge fields into an existing cached session (if present)."""
    entry = SESSION_CACHE.get(phone_number)
    if entry and time.monotonic() < entry["expires_at"]:
        entry["data"].update(fields)
        entry["expires_at"] = time.monotonic() + SESSION_CACHE_TTL


def _cache_invalidate(phone_number: str):
    """Remove a session from cache."""
    SESSION_CACHE.pop(phone_number, None)

# -------- MONGODB HELPER FUNCTIONS --------


async def get_or_create_session(phone_number: str) -> dict:
    cached = _cache_get(phone_number)
    if cached:
        return cached
    if sessions_collection is None:
        session = {"phone_number": phone_number, "history": [],
                   "last_activity": datetime.now(timezone.utc)}
        _cache_set(phone_number, session)
        return session
    try:
        session = await sessions_collection.find_one({"phone_number": phone_number})
        if session:
            await sessions_collection.update_one(
                {"phone_number": phone_number},
                {"$set": {"last_activity": datetime.now(timezone.utc)}}
            )
            _cache_set(phone_number, session)
            return session
        else:
            new_session = {
                "phone_number": phone_number,
                "history": [],
                "last_activity": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc)
            }
            await sessions_collection.insert_one(new_session)
            _cache_set(phone_number, new_session)
            return new_session
    except Exception:
        session = {"phone_number": phone_number, "history": [],
                   "last_activity": datetime.now(timezone.utc)}
        _cache_set(phone_number, session)
        return session


async def save_message_to_db(phone_number: str, sender: str, content: str, msg_type: str, response_time_ms: int = None):
    """Save message to permanent chat history"""
    if chats_collection is None:
        return

    current_time = datetime.now(timezone.utc)
    message = {
        "phone_number": phone_number,
        "sender": sender,
        "content": content,
        "timestamp": current_time,
        "type": msg_type
    }
    if response_time_ms is not None:
        message["response_time_ms"] = response_time_ms

    try:
        await chats_collection.insert_one(message)
    except Exception as e:
        print(f"⚠️ DB write failed, storing in memory: {e.__class__.__name__}")
        # Fallback to in-memory
        if phone_number not in MESSAGE_STORE:
            MESSAGE_STORE[phone_number] = []
        MESSAGE_STORE[phone_number].append({
            "sender": sender,
            "content": content,
            "timestamp": current_time.isoformat(),
            "type": msg_type
        })


async def get_chat_history(phone_number: str, limit: int = None):
    """Get chat history from MongoDB"""
    if chats_collection is None:
        return []

    cursor = chats_collection.find(
        {"phone_number": phone_number}
    ).sort("timestamp", 1)

    if limit:
        cursor = cursor.limit(limit)

    return await cursor.to_list(length=limit or None)


async def update_human_takeover(phone_number: str, status: bool):
    if sessions_collection is None:
        HUMAN_TAKEOVER[phone_number] = status
        return
    try:
        await sessions_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"human_takeover": status,
                      "last_activity": datetime.now(timezone.utc)}},
            upsert=True
        )
        _cache_update(phone_number, {"human_takeover": status})
    except Exception:
        HUMAN_TAKEOVER[phone_number] = status
        _cache_invalidate(phone_number)


async def get_human_takeover_status(phone_number: str) -> bool:
    cached = _cache_get(phone_number)
    if cached is not None:
        return cached.get("human_takeover", False)
    if sessions_collection is None:
        return HUMAN_TAKEOVER.get(phone_number, False)
    try:
        session = await sessions_collection.find_one({"phone_number": phone_number})
        if session:
            _cache_set(phone_number, session)
        return session.get("human_takeover", False) if session else False
    except Exception:
        return HUMAN_TAKEOVER.get(phone_number, False)


async def set_pending_confirmation(phone_number: str, status: bool):
    if sessions_collection is None:
        PENDING_HUMAN_CONFIRMATION[phone_number] = status
        return
    try:
        await sessions_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"pending_confirmation": status,
                      "last_activity": datetime.now(timezone.utc)}},
            upsert=True
        )
        _cache_update(phone_number, {"pending_confirmation": status})
    except Exception:
        PENDING_HUMAN_CONFIRMATION[phone_number] = status
        _cache_invalidate(phone_number)


async def get_pending_confirmation(phone_number: str) -> bool:
    cached = _cache_get(phone_number)
    if cached is not None:
        return cached.get("pending_confirmation", False)
    if sessions_collection is None:
        return PENDING_HUMAN_CONFIRMATION.get(phone_number, False)
    try:
        session = await sessions_collection.find_one({"phone_number": phone_number})
        if session:
            _cache_set(phone_number, session)
        return session.get("pending_confirmation", False) if session else False
    except Exception:
        return PENDING_HUMAN_CONFIRMATION.get(phone_number, False)


async def has_been_greeted(phone_number: str) -> bool:
    """Check if user has already received the greeting"""
    cached = _cache_get(phone_number)
    if cached is not None:
        return cached.get("greeted", False)
    if sessions_collection is None:
        return phone_number in USER_HISTORY and len(USER_HISTORY[phone_number]) > 0
    try:
        session = await sessions_collection.find_one({"phone_number": phone_number})
        if session:
            _cache_set(phone_number, session)
        return session.get("greeted", False) if session else False
    except Exception:
        return phone_number in USER_HISTORY and len(USER_HISTORY[phone_number]) > 0


async def set_greeted(phone_number: str):
    """Mark user as greeted"""
    if sessions_collection is None:
        return
    try:
        await sessions_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"greeted": True,
                      "last_activity": datetime.now(timezone.utc)}},
            upsert=True
        )
        _cache_update(phone_number, {"greeted": True})
    except Exception as e:
        print(f"⚠️ set_greeted DB write failed: {e.__class__.__name__}")


async def set_kb_injected(phone_number: str, status: bool = True):
    """Mark knowledge base injection status for this session"""
    if sessions_collection is None:
        KB_INJECTED[phone_number] = status
        return
    await sessions_collection.update_one(
        {"phone_number": phone_number},
        {"$set": {"kb_injected": status,
                  "last_activity": datetime.now(timezone.utc)}},
        upsert=True
    )


async def get_kb_check_info(phone_number: str) -> dict:
    """Return stored kb_hash and kb_last_checked for this user session."""
    cached = _cache_get(phone_number)
    if cached is not None:
        return {
            "kb_hash": cached.get("kb_hash"),
            "kb_last_checked": cached.get("kb_last_checked")
        }
    if sessions_collection is None:
        return {"kb_hash": None, "kb_last_checked": None}
    try:
        session = await sessions_collection.find_one({"phone_number": phone_number})
        if session:
            _cache_set(phone_number, session)
            return {
                "kb_hash": session.get("kb_hash"),
                "kb_last_checked": session.get("kb_last_checked")
            }
    except Exception:
        pass
    return {"kb_hash": None, "kb_last_checked": None}


async def update_kb_check_info(phone_number: str, kb_hash: str):
    """Store the latest KB hash and check timestamp for this user."""
    now = datetime.now(timezone.utc)
    if sessions_collection is None:
        return
    try:
        await sessions_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "kb_hash": kb_hash,
                "kb_last_checked": now,
                "last_activity": now
            }},
            upsert=True
        )
        _cache_update(
            phone_number, {"kb_hash": kb_hash, "kb_last_checked": now})
    except Exception as e:
        print(f"⚠️ update_kb_check_info failed: {e.__class__.__name__}")

# -------- LOAD PERSISTENT DATA (Fallback) --------


def load_data():
    """Load conversations from file (fallback if MongoDB fails)"""
    global MESSAGE_STORE, LAST_USER_MESSAGE_TIME, HUMAN_TAKEOVER

    if mongo_client is not None:
        print("✅ Using MongoDB, skipping JSON file load")
        return

    if STORAGE_FILE.exists():
        try:
            with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                MESSAGE_STORE = data.get('messages', {})
                HUMAN_TAKEOVER = data.get('human_takeover', {})
                last_times = data.get('last_message_times', {})
                LAST_USER_MESSAGE_TIME = {
                    phone: datetime.fromisoformat(time_str)
                    for phone, time_str in last_times.items()
                }
            print(
                f"✅ Loaded {len(MESSAGE_STORE)} conversations from JSON file")
        except Exception as e:
            print(f"⚠️ Error loading data: {e}")


def save_data():
    """Save conversations to file (fallback if MongoDB fails)"""
    if mongo_client is not None:
        return  # MongoDB handles persistence

    try:
        data = {
            'messages': MESSAGE_STORE,
            'human_takeover': HUMAN_TAKEOVER,
            'last_message_times': {
                phone: time.isoformat()
                for phone, time in LAST_USER_MESSAGE_TIME.items()
            }
        }
        with open(STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Error saving data: {e}")


# Load data on startup
load_data()

# -------- IMAGE REQUEST DETECTION --------


def is_image_request(text: str) -> bool:
    return any(
        word in text.lower()
        for word in ["image", "images", "photo", "photos", "picture", "show"]
    )


# -------- GREETING DETECTION --------
GREETING_KEYWORDS = {
    "hi", "hlo", "helo", "hello", "hey", "hii", "hiii", "sup", "yo",
    "good morning", "good afternoon", "good evening", "howdy", "greetings",
    "namaste", "salam", "salaam", "bonjour", "hola"
}


def is_greeting(text: str) -> bool:
    """Return True if the message is just a greeting with no other intent."""
    cleaned = text.lower().strip().rstrip("!.,?")
    return cleaned in GREETING_KEYWORDS

# -------- HUMAN REQUEST DETECTION --------


def is_human_request(text: str) -> bool:
    """Detect if user is asking to speak to a human"""
    human_keywords = [
        "human", "representative", "executive",
        "speak to a person", "talk to a person",
        "speak to someone", "talk to someone",
        "connect me to", "transfer me to",
        "real person", "live agent", "human support"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in human_keywords)


def is_confirmation_response(text: str) -> tuple[bool, bool]:
    """
    Check if message is a yes/no response
    Returns: (is_response, is_yes)
    """
    text_lower = text.lower().strip()

    # Yes responses
    yes_keywords = ["yes", "yeah", "yep", "sure", "ok",
                    "okay", "fine", "please", "connect", "proceed"]
    # No responses
    no_keywords = ["no", "nope", "nah", "not now",
                   "later", "cancel", "nevermind", "never mind"]

    is_yes = any(keyword in text_lower for keyword in yes_keywords)
    is_no = any(keyword in text_lower for keyword in no_keywords)

    if is_yes:
        return (True, True)
    elif is_no:
        return (True, False)
    else:
        return (False, False)

# -------- STORE MESSAGE (NON-BLOCKING) --------


async def store_message(phone_number: str, sender: str, content: str, msg_type: str, response_time_ms: int = None):
    """Store message in MongoDB or fallback storage"""
    if mongo_client is not None:
        await save_message_to_db(phone_number, sender, content, msg_type, response_time_ms)
    else:
        # Fallback to in-memory storage
        if phone_number not in MESSAGE_STORE:
            MESSAGE_STORE[phone_number] = []
        entry = {
            "sender": sender,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": msg_type
        }
        if response_time_ms is not None:
            entry["response_time_ms"] = response_time_ms
        MESSAGE_STORE[phone_number].append(entry)

# -------- CHATGPT --------


async def ask_chatgpt(user_number: str, user_message: str) -> str:
    if user_number not in USER_HISTORY:
        USER_HISTORY[user_number] = deque(maxlen=MAX_HISTORY)

    # --- FAQ CACHE CHECK: serve identical questions instantly ---
    faq_key = " ".join(user_message.lower().strip().split())
    if is_faq_cacheable(user_message):
        cached_reply = faq_cache_get(faq_key)
        if cached_reply:
            print(f"⚡ CACHE HIT: {faq_key[:60]}")
            print(f"⚡ CACHE HIT - RESPONSE SERVED WITHOUT OPENAI")
            # Still append to history so conversation context stays intact
            USER_HISTORY[user_number].append(
                {"role": "user", "content": user_message})
            USER_HISTORY[user_number].append(
                {"role": "assistant", "content": cached_reply})
            return cached_reply
        else:
            print(f"🔍 CACHE MISS: {faq_key[:60]}")

    USER_HISTORY[user_number].append({"role": "user", "content": user_message})

    # System prompt sent every turn
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(USER_HISTORY[user_number])

    # --- INFLIGHT DEDUPLICATION: reuse active identical requests ---
    inflight_key = f"{user_number}:{user_message}"
    if inflight_key in INFLIGHT_REQUESTS:
        print(f"⚡ [{user_number}] Reusing inflight OpenAI request")
        try:
            return await asyncio.shield(INFLIGHT_REQUESTS[inflight_key])
        except Exception:
            pass  # Fall through to make a fresh call

    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    INFLIGHT_REQUESTS[inflight_key] = future

    try:
        print(f"🤖 OPENAI REQUEST START [{user_number}]")
        openai_start = time.monotonic()
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            timeout=TIMEOUT
        )
        elapsed = time.monotonic() - openai_start
        print(f"✅ OPENAI REQUEST END - {elapsed:.2f} seconds")

        reply = response.choices[0].message.content.strip()
        USER_HISTORY[user_number].append(
            {"role": "assistant", "content": reply})
        future.set_result(reply)

        # Store in FAQ cache if eligible
        if is_faq_cacheable(user_message):
            faq_cache_set(faq_key, reply)

        return reply
    except Exception as e:
        print(f"❌ OpenAI API error: {str(e)[:100]}")
        future.set_exception(e)
        raise
    finally:
        INFLIGHT_REQUESTS.pop(inflight_key, None)


@app.post("/whatsappDemo")
async def send_whatsapp_demo(request: Request):
    sid = config.CONTENT_SID

    data = await request.json()
    raw_number = data.get("phone_number", "").strip()

    # Format number correctly: whatsapp:+[country_code][number]
    clean_number = raw_number.replace("whatsapp:", "").strip()
    if not clean_number.startswith("+"):
        clean_number = f"+{clean_number}"
    formatted_target = f"whatsapp:{clean_number}"

    try:
        # STRICT SEND: Forcing the Template
        # This works for both new users (outside window) and old users (inside window)
        message = twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=formatted_target,
            content_sid=sid  # This is the key to fixing 63016
        )
        print(f"✅ Success for {formatted_target}! SID: {message.sid}")
        return {"success": True, "sid": message.sid}
    except Exception as e:
        print(f"❌ Error for {formatted_target}: {e}")
        return {"success": False, "error": str(e)}


@app.post("/sendFormTemplate")
async def send_form_template(request: Request):
    """Send the form-sending WhatsApp template, with automatic SMS fallback.
    Used by the voice agent and the dashboard to deliver the requirements/booking form."""
    form_sid = config.FORM_SENDING_SID

    data = await request.json()
    raw_number = data.get("phone_number", "").strip()
    call_id = data.get("call_id")  # Optional: to correlate with a call log

    # Format number correctly: whatsapp:+[country_code][number]
    clean_number = raw_number.replace("whatsapp:", "").strip()
    if not clean_number.startswith("+"):
        clean_number = f"+{clean_number}"
    formatted_target = f"whatsapp:{clean_number}"

    if not twilio_client:
        return {"success": False, "error": "Twilio client not configured", "phone_number": clean_number, "call_id": call_id}
    if not form_sid:
        return {"success": False, "error": "FORM_SENDING_SID not configured", "phone_number": clean_number, "call_id": call_id}

    try:
        message = twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=formatted_target,
            content_sid=form_sid,
        )
        print(
            f"✅ Form template sent to {formatted_target}! SID: {message.sid}")
        return {
            "success": True,
            "sid": message.sid,
            "channel": "whatsapp",
            "phone_number": clean_number,
            "call_id": call_id,
            "message": "WhatsApp sent successfully",
        }
    except Exception as e:
        error_str = str(e)
        print(f"❌ WhatsApp API error for {formatted_target}: {e}")

        # Immediate SMS fallback for known WhatsApp delivery errors
        if "63016" in error_str or "21211" in error_str:
            try:
                sms_from = os.getenv(
                    "TWILIO_SMS_NUMBER") or TWILIO_WHATSAPP_NUMBER
                sms_message = twilio_client.messages.create(
                    from_=sms_from.replace("whatsapp:", ""),
                    to=clean_number,
                    body="We are sending this from Autonomiq. Kindly fill this form: https://autonomiq.ae/company-details",
                )
                print(
                    f"✅ SMS fallback sent to {clean_number}! SID: {sms_message.sid}")
                return {
                    "success": True,
                    "sid": sms_message.sid,
                    "channel": "sms",
                    "fallback": True,
                    "phone_number": clean_number,
                    "call_id": call_id,
                    "message": "WhatsApp failed, SMS sent instead",
                }
            except Exception as sms_error:
                print(f"❌ SMS fallback also failed: {sms_error}")
                return {"success": False, "error": f"Both WhatsApp and SMS failed: {sms_error}", "phone_number": clean_number, "call_id": call_id}

        return {"success": False, "error": error_str, "phone_number": clean_number, "call_id": call_id}

# -------- SEND TYPING INDICATOR --------


async def send_typing_indicator(to_number: str):
    """Send typing indicator to show bot is processing"""
    if not twilio_client:
        return

    try:
        # Send a reaction or empty message to trigger typing indicator
        # Note: WhatsApp Business API has limited typing indicator support
        # This is a workaround that works for some Twilio accounts
        await asyncio.to_thread(
            twilio_client.messages.create,
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=to_number,
            body=""  # Empty body can trigger typing in some cases
        )
    except Exception as e:
        print(f"Typing indicator failed (non-critical): {e}")

# -------- WHATSAPP WEBHOOK --------


@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    webhook_start = time.time()

    form = await request.form()
    user_message = form.get("Body", "").strip()
    user_number = form.get("From", "")

    print(f"\n📨 [{user_number}] User: \"{user_message}\"")

    # Track when user last messaged (for 24-hour window)
    LAST_USER_MESSAGE_TIME[user_number] = datetime.now(timezone.utc)
    webhook_receive_time = time.time()  # Start timer for response time tracking

    # Store user message to permanent chat history
    await store_message(user_number, "user", user_message, "user")

    resp = MessagingResponse()

    # Check if human has taken over
    if await get_human_takeover_status(user_number):
        print(
            f"🧑 Human mode active for {user_number}, not sending AI response")
        return Response(content=str(resp), media_type="text/xml")

    try:
        # --- FIRST MESSAGE: inject KB into history + send greeting once, then return ---
        already_greeted = await has_been_greeted(user_number)
        if not already_greeted:
            if user_number not in USER_HISTORY:
                USER_HISTORY[user_number] = deque(maxlen=MAX_HISTORY)
            # Inject KB once into history
            USER_HISTORY[user_number].appendleft(
                {"role": "assistant",
                    "content": "Understood. I have the product knowledge ready."}
            )
            USER_HISTORY[user_number].appendleft(
                {"role": "user",
                    "content": f"[KNOWLEDGE BASE]\n\n{KNOWLEDGE_BASE}"}
            )
            try:
                await asyncio.to_thread(
                    twilio_client.messages.create,
                    **{
                        "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                        "to": user_number,
                        "body": GREETING_MESSAGE
                    }
                )
                greet_time_ms = int(
                    (time.time() - webhook_receive_time) * 1000)
                await store_message(user_number, "agent", GREETING_MESSAGE, "ai", greet_time_ms)
                await set_greeted(user_number)
                await update_kb_check_info(user_number, get_kb_hash())
                print(
                    f"👋 [{user_number}] Greeting sent in {greet_time_ms / 1000:.2f}s")
            except Exception as greet_err:
                print(f"⚠️ [{user_number}] Greeting failed: {greet_err}")
            return Response(content=str(resp), media_type="text/xml")

        # --- RETURNING USER: re-inject KB if server restarted and history is empty ---
        if user_number not in USER_HISTORY or len(USER_HISTORY[user_number]) == 0:
            if user_number not in USER_HISTORY:
                USER_HISTORY[user_number] = deque(maxlen=MAX_HISTORY)
            USER_HISTORY[user_number].appendleft(
                {"role": "assistant",
                    "content": "Understood. I have the product knowledge ready."}
            )
            USER_HISTORY[user_number].appendleft(
                {"role": "user",
                    "content": f"[KNOWLEDGE BASE]\n\n{KNOWLEDGE_BASE}"}
            )
            # Record hash and check time for freshly injected KB
            await update_kb_check_info(user_number, get_kb_hash())
        else:
            # --- KB FRESHNESS CHECK: every 24 hours, re-check if KB changed ---
            kb_info = await get_kb_check_info(user_number)
            last_checked = kb_info.get("kb_last_checked")
            stored_hash = kb_info.get("kb_hash")
            now = datetime.now(timezone.utc)
            due_for_check = (
                last_checked is None or
                (now - (last_checked.replace(tzinfo=timezone.utc)
                 if last_checked.tzinfo is None else last_checked)) >= timedelta(hours=KB_CHECK_INTERVAL_HOURS)
            )
            if due_for_check:
                fresh_kb = reload_knowledge_base()
                current_hash = get_kb_hash()
                if current_hash != stored_hash:
                    # KB changed — replace the injected KB messages in history
                    print(
                        f"🔄 [{user_number}] KB changed, re-injecting updated knowledge base")
                    # Remove old KB messages (first two items in history are user KB + assistant ack)
                    history_list = list(USER_HISTORY[user_number])
                    # Drop stale KB entries (they are always the first two)
                    if len(history_list) >= 2 and "[KNOWLEDGE BASE]" in history_list[0].get("content", ""):
                        history_list = history_list[2:]
                    new_history = deque(history_list, maxlen=MAX_HISTORY)
                    new_history.appendleft(
                        {"role": "assistant",
                            "content": "Understood. I have the product knowledge ready."}
                    )
                    new_history.appendleft(
                        {"role": "user",
                            "content": f"[KNOWLEDGE BASE]\n\n{fresh_kb}"}
                    )
                    USER_HISTORY[user_number] = new_history
                else:
                    print(f"✅ [{user_number}] KB unchanged after 24h check")
                # Always update the last-checked timestamp
                await update_kb_check_info(user_number, current_hash)

        # Check if we're waiting for confirmation
        if await get_pending_confirmation(user_number):
            is_response, is_yes = is_confirmation_response(user_message)

            if is_response:
                if is_yes:
                    # User confirmed - send template and activate human takeover
                    print(f"✅ User confirmed human connection: {user_number}")

                    try:
                        # Send template to customer
                        await asyncio.to_thread(
                            twilio_client.messages.create,
                            **{
                                "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                                "to": user_number,
                                "content_sid": HUMAN_TAKEOVER_SID
                            }
                        )

                        # Store template message
                        takeover_time_ms = int(
                            (time.time() - webhook_receive_time) * 1000)
                        await store_message(user_number, "agent", "Connecting you to a support executive...", "ai", takeover_time_ms)
                        print(
                            f"✅ [{user_number}] Human takeover sent in {takeover_time_ms / 1000:.2f}s")

                        # Send alert to owner
                        try:
                            await asyncio.to_thread(
                                twilio_client.messages.create,
                                **{
                                    "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                                    "to": f"whatsapp:{OWNER_WHATSAPP_NUMBER}",
                                    "content_sid": OWNER_ALERT_SID,
                                    "content_variables": json.dumps({"1": user_number})
                                }
                            )
                        except Exception as alert_error:
                            print(
                                f"⚠️ [{user_number}] Owner alert failed: {alert_error}")

                        # Activate human takeover
                        await update_human_takeover(user_number, True)
                        await set_pending_confirmation(user_number, False)

                        # Return empty response since template was sent
                        return Response(content=str(resp), media_type="text/xml")

                    except Exception as template_error:
                        print(
                            f"❌ [{user_number}] Human takeover template failed: {template_error}")
                        error_msg = "Sorry, I couldn't connect you right now. Please try again."
                        err_time_ms = int(
                            (time.time() - webhook_receive_time) * 1000)
                        await store_message(user_number, "agent", error_msg, "ai", err_time_ms)
                        resp.message().body(error_msg)
                        await set_pending_confirmation(user_number, False)
                        return Response(content=str(resp), media_type="text/xml")
                else:
                    # User said no - continue with AI and DON'T check for human request again
                    print(f"❌ User declined human connection: {user_number}")
                    await set_pending_confirmation(user_number, False)

                    # Check if user included a question in their "no" response
                    # e.g., "No, I need to know about company more"
                    if len(user_message.split()) > 3:  # More than just "no" or "nope"
                        # User included additional text, process it as a question
                        print(
                            f"📝 User declined and asked a question: {user_message}")

                        reply = await ask_chatgpt(user_number, user_message)
                        reply_time_ms = int(
                            (time.time() - webhook_receive_time) * 1000)
                        await store_message(user_number, "agent", reply, "ai", reply_time_ms)
                        resp.message().body(reply)
                    else:
                        # Just a simple "no"
                        confirmation_msg = "No problem! I'll continue helping you. What can I assist you with?"
                        conf_time_ms = int(
                            (time.time() - webhook_receive_time) * 1000)
                        await store_message(user_number, "agent", confirmation_msg, "ai", conf_time_ms)
                        resp.message().body(confirmation_msg)

                    return Response(content=str(resp), media_type="text/xml")
            else:
                # Not a clear yes/no - treat as a new question, clear pending state, and process normally
                print(
                    f"⚠️ User sent different message while pending confirmation, clearing state: {user_message}")
                await set_pending_confirmation(user_number, False)
                # Continue to normal AI processing (don't check for human request again)
                # This prevents false triggers when user asks new questions after saying "no"

        # Only check for human request if NOT coming from a pending confirmation state
        elif is_human_request(user_message):
            print(f"👤 User requested human agent: {user_number}")

            # Ask for confirmation
            await set_pending_confirmation(user_number, True)
            confirmation_msg = "Would you like me to connect you to a support executive?"

            conf_time_ms = int((time.time() - webhook_receive_time) * 1000)
            await store_message(user_number, "agent", confirmation_msg, "ai", conf_time_ms)
            resp.message().body(confirmation_msg)

            print(f"❓ Confirmation request sent to {user_number}")
            return Response(content=str(resp), media_type="text/xml")

        # --- GREETING SHORTCUT: bypass LLM for pure greeting messages ---
        if is_greeting(user_message):
            reply = "Hello! What brings you here today? How can I help you?"
            print(f"👋 [{user_number}] Greeting shortcut used")
        else:
            reply = await ask_chatgpt(user_number, user_message)

        # Store AI message with response time
        reply_time_ms = int((time.time() - webhook_receive_time) * 1000)
        await store_message(user_number, "agent", reply, "ai", reply_time_ms)
        print(f"✅ [{user_number}] Agent replied in {reply_time_ms / 1000:.2f}s")

        resp.message().body(reply)

    except Exception as e:
        print("ERROR:", e)
        error_msg = "I'm sorry, I hit a snag. Try again?"

        # Store error message with response time
        err_time_ms = int((time.time() - webhook_receive_time) * 1000)
        await store_message(user_number, "agent", error_msg, "ai", err_time_ms)
        print(
            f"❌ [{user_number}] Error response sent in {err_time_ms / 1000:.2f}s — {e}")

        resp.message().body(error_msg)

    return Response(
        content=str(resp),
        media_type="text/xml"
    )


# -------- KB INJECTION TEST ENDPOINT --------
@app.get("/test-kb/{phone}")
async def test_kb(phone: str):
    """Test endpoint to verify KB injection is working correctly"""
    phone = phone.replace("%3A", ":")
    injected = await is_kb_injected(phone)
    history = list(USER_HISTORY.get(phone, []))
    return {
        "kb_injected_in_db": injected,
        "history_length_in_memory": len(history),
        "first_message_role": history[0]["role"] if history else None,
        "first_message_preview": history[0]["content"][:80] if history else None,
        "second_message_role": history[1]["role"] if len(history) > 1 else None,
        "second_message_preview": history[1]["content"][:80] if len(history) > 1 else None,
    }


@app.post("/test-chat")
async def test_chat(request: Request):
    """Test endpoint to simulate a WhatsApp message without Twilio"""
    data = await request.json()
    phone = data.get("phone", "test:+911234567890")
    message = data.get("message", "Hello")
    reply = await ask_chatgpt(phone, message)
    history = list(USER_HISTORY.get(phone, []))
    return {
        "reply": reply,
        "history_length": len(history),
        "prompt_source": "prompt.txt",
        "first_message_preview": history[0]["content"][:80] if history else None
    }

# -------- DASHBOARD API ENDPOINTS --------


@app.get("/conversations")
async def get_conversations():
    """Get all active conversations — cached for 30 seconds"""
    now = time.monotonic()
    if CONVERSATIONS_CACHE["data"] is not None and now < CONVERSATIONS_CACHE["expires_at"]:
        return CONVERSATIONS_CACHE["data"]

    conversations = []

    if mongo_client is not None:
        sessions = sessions_collection.find({})
        async for session in sessions:
            phone_number = session["phone_number"]

            last_chat = await chats_collection.find_one(
                {"phone_number": phone_number},
                sort=[("timestamp", -1)]
            )

            timestamp_str = ""
            if last_chat:
                iso_time = last_chat["timestamp"].isoformat()
                if not iso_time.endswith(('Z', '+00:00')):
                    timestamp_str = iso_time + "+00:00"
                else:
                    timestamp_str = iso_time

            conversations.append({
                "phone_number": phone_number,
                "human_takeover": session.get("human_takeover", False),
                "last_message": last_chat["content"] if last_chat else "",
                "last_message_time": timestamp_str
            })
    else:
        for phone_number in MESSAGE_STORE.keys():
            messages = MESSAGE_STORE[phone_number]
            last_message = messages[-1] if messages else None
            conversations.append({
                "phone_number": phone_number,
                "human_takeover": HUMAN_TAKEOVER.get(phone_number, False),
                "last_message": last_message["content"] if last_message else "",
                "last_message_time": last_message["timestamp"] if last_message else ""
            })

    CONVERSATIONS_CACHE["data"] = conversations
    CONVERSATIONS_CACHE["expires_at"] = now + 30  # 30-second TTL
    return conversations


@app.get("/messages/{phone_number}")
async def get_messages(phone_number: str):
    """Get all messages for a specific conversation"""
    # Handle URL encoding
    phone_number = phone_number.replace("%3A", ":")

    if mongo_client is not None:
        # Get from MongoDB - get ALL messages (no limit)
        messages = await get_chat_history(phone_number, limit=None)

        result = [{
            "sender": msg["sender"],
            "content": msg["content"],
            "timestamp": msg["timestamp"].isoformat() + "+00:00" if not msg["timestamp"].isoformat().endswith(('Z', '+00:00')) else msg["timestamp"].isoformat(),
            "type": msg["type"],
            # None for user messages
            "response_time_ms": msg.get("response_time_ms")
        } for msg in messages]

        return result
    else:
        # Fallback to in-memory storage
        return MESSAGE_STORE.get(phone_number, [])


@app.post("/takeover")
async def takeover_conversation(request: Request):
    """Human agent takes over the conversation and sends template message"""
    data = await request.json()
    phone_number = data.get("phone_number")

    if not phone_number:
        raise HTTPException(status_code=400, detail="phone_number is required")

    await update_human_takeover(phone_number, True)
    print(f"🧑 Human takeover activated for {phone_number}")
    CONVERSATIONS_CACHE["data"] = None  # Invalidate dashboard cache
    try:
        twilio_message = await asyncio.to_thread(
            twilio_client.messages.create,
            **{
                "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                "to": phone_number,
                "content_sid": HUMAN_TAKEOVER_SID
            }
        )

        # Store template message
        await store_message(phone_number, "agent", "Human agent has joined the conversation (template sent)", "human")

        print(
            f"✅ Template sent automatically on takeover, SID: {twilio_message.sid}")
        return {
            "success": True,
            "message": "Takeover successful, template sent",
            "template_sent": True,
            "sid": twilio_message.sid
        }
    except Exception as e:
        print(f"⚠️ Takeover successful but template failed: {e}")
        return {
            "success": True,
            "message": "Takeover successful but template failed to send",
            "template_sent": False,
            "error": str(e)
        }


@app.post("/release")
async def release_conversation(request: Request):
    """Release conversation back to AI"""
    data = await request.json()
    phone_number = data.get("phone_number")

    if not phone_number:
        raise HTTPException(status_code=400, detail="phone_number is required")

    await update_human_takeover(phone_number, False)
    print(f"🤖 AI mode restored for {phone_number}")
    CONVERSATIONS_CACHE["data"] = None  # Invalidate dashboard cache

    return {"success": True, "message": "Released to AI"}


@app.post("/send-message")
async def send_message(request: Request):
    """Human agent sends a message"""
    data = await request.json()
    phone_number = data.get("phone_number")
    message = data.get("message")
    use_template = data.get("use_template", False)

    if not phone_number or not message:
        raise HTTPException(
            status_code=400, detail="phone_number and message are required")

    # Check if human has taken over
    if not await get_human_takeover_status(phone_number):
        raise HTTPException(
            status_code=403, detail="Must take over conversation first")

    try:
        if use_template:
            twilio_message = await asyncio.to_thread(
                twilio_client.messages.create,
                **{
                    "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                    "to": phone_number,
                    "content_sid": CONTENT_SID
                }
            )
        else:
            twilio_message = await asyncio.to_thread(
                twilio_client.messages.create,
                **{
                    "from_": f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                    "to": phone_number,
                    "body": message
                }
            )

        # Store message
        await store_message(phone_number, "agent", message, "human")

        print(f"✅ Human agent sent message to {phone_number}")
        return {"success": True, "message": "Message sent", "sid": twilio_message.sid}

    except Exception as e:
        error_str = str(e)
        print(f"❌ Error sending message: {error_str}")

        # Check if it's the 63016 error (outside messaging window)
        if "63016" in error_str:
            return {
                "success": False,
                "error": "Outside 24-hour messaging window. User must message you first, or use an approved template.",
                "error_code": "63016"
            }

        raise HTTPException(status_code=500, detail=error_str)
