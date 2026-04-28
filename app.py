import os
import asyncio
import time
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
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure

# ---------------- CONFIG ----------------
MAX_HISTORY = 1  # Reduced to 1 for faster processing
MODEL_NAME = "gpt-4o-mini"  # Cheaper and good quality
MAX_TOKENS = 150  # Reduced for speed while maintaining completeness
TIMEOUT = 10  # Slightly increased for longer responses
TEMPERATURE = 0.5  # Higher for faster generation
SESSION_TIMEOUT_HOURS = 24  # Session expires after 24 hours of inactivity
# ----------------------------------------

load_dotenv()
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- MONGODB CONNECTION ---
MONGODB_URL = os.getenv("MONGODB_URL")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "WhatsappChat24hrs")

mongo_client = None
db = None
sessions_collection = None
chats_collection = None

try:
    mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command('ping')
    db = mongo_client[MONGODB_DATABASE]
    sessions_collection = db['sessions']
    chats_collection = db['chats']
    
    # Create TTL index on sessions collection (auto-delete after 24 hours)
    sessions_collection.create_index(
        "last_activity",
        expireAfterSeconds=SESSION_TIMEOUT_HOURS * 3600
    )
    
    # Create index on chats for faster queries
    chats_collection.create_index([("phone_number", ASCENDING), ("timestamp", ASCENDING)])
    
    print(f"✅ Connected to MongoDB: {MONGODB_DATABASE}")
except ConnectionFailure as e:
    print(f"❌ MongoDB connection failed: {e}")
    print("⚠️ Falling back to JSON file storage")
except Exception as e:
    print(f"❌ MongoDB setup error: {e}")
    print("⚠️ Falling back to JSON file storage")

# --- PROFESSIONAL CREDENTIALS ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # Your registered business number
CONTENT_SID = os.getenv("CONTENT_SID") # The 'HX...' ID from Content Editor
HUMAN_TAKEOVER_SID = os.getenv("HUMAN_TAKEOVER_SID") # Template for human takeover

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        print(f"Warning: Could not initialize Twilio client: {e}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- LOAD KNOWLEDGE BASE PROMPT --------
with open("prompt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

# -------- MEMORY PER USER --------
USER_HISTORY = {}

# -------- HUMAN TAKEOVER STATE --------
HUMAN_TAKEOVER = {}  # {phone_number: True/False}

# -------- MESSAGE STORAGE (Fallback for non-MongoDB) --------
MESSAGE_STORE = {}  # {phone_number: [{sender, content, timestamp, type}]}
LAST_USER_MESSAGE_TIME = {}  # Track when user last messaged
STORAGE_FILE = Path("conversation_data.json")

# -------- MONGODB HELPER FUNCTIONS --------
def get_or_create_session(phone_number: str) -> dict:
    """Get existing session or create new one, load chat history"""
    if sessions_collection is None:
        return {"phone_number": phone_number, "history": [], "last_activity": datetime.now(timezone.utc)}
    
    # Check if session exists and is not expired
    session = sessions_collection.find_one({"phone_number": phone_number})
    
    if session:
        # Session exists, update last activity
        sessions_collection.update_one(
            {"phone_number": phone_number},
            {"$set": {"last_activity": datetime.now(timezone.utc)}}
        )
        print(f"✅ Existing session found for {phone_number}")
        return session
    else:
        # Session expired or doesn't exist, create new one
        # But load chat history from permanent storage
        chat_history = list(chats_collection.find(
            {"phone_number": phone_number}
        ).sort("timestamp", -1).limit(10))  # Load last 10 messages
        
        new_session = {
            "phone_number": phone_number,
            "history": [],  # Fresh conversation history for AI
            "last_activity": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc)
        }
        
        sessions_collection.insert_one(new_session)
        
        if chat_history:
            print(f"✅ New session created for {phone_number}, loaded {len(chat_history)} previous messages")
        else:
            print(f"✅ New session created for {phone_number} (first time user)")
        
        return new_session

def save_message_to_db(phone_number: str, sender: str, content: str, msg_type: str):
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
    
    print(f"💾 Storing message with timestamp: {current_time.isoformat()}")
    chats_collection.insert_one(message)

def get_chat_history(phone_number: str, limit: int = 50):
    """Get chat history from MongoDB"""
    if chats_collection is None:
        return []
    
    return list(chats_collection.find(
        {"phone_number": phone_number}
    ).sort("timestamp", 1).limit(limit))

def update_human_takeover(phone_number: str, status: bool):
    """Update human takeover status in session"""
    if sessions_collection is None:
        HUMAN_TAKEOVER[phone_number] = status
        return
    
    sessions_collection.update_one(
        {"phone_number": phone_number},
        {"$set": {"human_takeover": status, "last_activity": datetime.now(timezone.utc)}},
        upsert=True
    )

def get_human_takeover_status(phone_number: str) -> bool:
    """Check if human has taken over"""
    if sessions_collection is None:
        return HUMAN_TAKEOVER.get(phone_number, False)
    
    session = sessions_collection.find_one({"phone_number": phone_number})
    return session.get("human_takeover", False) if session else False

def set_pending_confirmation(phone_number: str, status: bool):
    """Set pending confirmation status"""
    if sessions_collection is None:
        PENDING_HUMAN_CONFIRMATION[phone_number] = status
        return
    
    sessions_collection.update_one(
        {"phone_number": phone_number},
        {"$set": {"pending_confirmation": status, "last_activity": datetime.now(timezone.utc)}},
        upsert=True
    )

def get_pending_confirmation(phone_number: str) -> bool:
    """Check if waiting for user confirmation"""
    if sessions_collection is None:
        return PENDING_HUMAN_CONFIRMATION.get(phone_number, False)
    
    session = sessions_collection.find_one({"phone_number": phone_number})
    return session.get("pending_confirmation", False) if session else False

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
            print(f"✅ Loaded {len(MESSAGE_STORE)} conversations from JSON file")
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

# -------- HUMAN REQUEST DETECTION --------
def is_human_request(text: str) -> bool:
    """Detect if user is asking to speak to a human"""
    human_keywords = [
        "human", "person", "agent", "representative", "executive",
        "department", "manager", "staff", "employee", "someone",
        "speak to", "talk to", "connect me", "transfer me"
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
    yes_keywords = ["yes", "yeah", "yep", "sure", "ok", "okay", "fine", "please", "connect", "proceed"]
    # No responses
    no_keywords = ["no", "nope", "nah", "not now", "later", "cancel", "nevermind", "never mind"]
    
    is_yes = any(keyword in text_lower for keyword in yes_keywords)
    is_no = any(keyword in text_lower for keyword in no_keywords)
    
    if is_yes:
        return (True, True)
    elif is_no:
        return (True, False)
    else:
        return (False, False)

# -------- STORE MESSAGE (NON-BLOCKING) --------
async def store_message(phone_number: str, sender: str, content: str, msg_type: str):
    """Store message in MongoDB or fallback storage"""
    if mongo_client is not None:
        # Save to MongoDB
        save_message_to_db(phone_number, sender, content, msg_type)
    else:
        # Fallback to in-memory storage
        if phone_number not in MESSAGE_STORE:
            MESSAGE_STORE[phone_number] = []
        MESSAGE_STORE[phone_number].append({
            "sender": sender,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": msg_type
        })

# -------- CHATGPT WITH PROMPT-ONLY KNOWLEDGE --------
async def ask_chatgpt(user_number: str, user_message: str) -> str:
    start_time = time.time()
    
    # Get or create session (handles expiration automatically)
    session = get_or_create_session(user_number)
    
    if user_number not in USER_HISTORY:
        USER_HISTORY[user_number] = deque(maxlen=MAX_HISTORY)

    USER_HISTORY[user_number].append(
        {"role": "user", "content": user_message}
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        }
    ]

    messages.extend(USER_HISTORY[user_number])

    api_start = time.time()
    print(f"⏱️ Starting OpenAI API call...")
    
    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            timeout=TIMEOUT,
            top_p=0.9  # Reduce sampling for faster generation
        )
        
        api_time = time.time() - api_start
        print(f"⏱️ OpenAI API took: {api_time:.2f} seconds")
    except Exception as e:
        api_time = time.time() - api_start
        print(f"❌ OpenAI API failed after {api_time:.2f}s: {str(e)[:100]}")
        raise

    reply = response.choices[0].message.content.strip()

    USER_HISTORY[user_number].append(
        {"role": "assistant", "content": reply}
    )

    total_time = time.time() - start_time
    print(f"⏱️ Total ask_chatgpt time: {total_time:.2f} seconds")
    
    return reply

@app.post("/whatsappDemo")
async def send_whatsapp_demo(request: Request):
    # Always reload to ensure the latest SID is used
    load_dotenv(override=True)
    sid = os.getenv("CONTENT_SID")
    
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
    
    print(f"\n🔵 New message from {user_number}: {user_message}")

    # Track when user last messaged (for 24-hour window)
    LAST_USER_MESSAGE_TIME[user_number] = datetime.now(timezone.utc)

    # Store user message to permanent chat history
    await store_message(user_number, "user", user_message, "user")
    
    resp = MessagingResponse()

    # Check if human has taken over
    if get_human_takeover_status(user_number):
        print(f"🧑 Human mode active for {user_number}, not sending AI response")
        return Response(content=str(resp), media_type="text/xml")

    try:
        # Check if we're waiting for confirmation
        if get_pending_confirmation(user_number):
            is_response, is_yes = is_confirmation_response(user_message)
            
            if is_response:
                if is_yes:
                    # User confirmed - send template and activate human takeover
                    print(f"✅ User confirmed human connection: {user_number}")
                    
                    try:
                        twilio_message = twilio_client.messages.create(
                            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                            to=user_number,
                            content_sid=HUMAN_TAKEOVER_SID
                        )
                        
                        # Store template message
                        await store_message(user_number, "agent", "Connecting you to a support executive...", "ai")
                        
                        print(f"✅ Human takeover template sent, SID: {twilio_message.sid}")
                        
                        # Activate human takeover
                        update_human_takeover(user_number, True)
                        set_pending_confirmation(user_number, False)
                        
                        # Return empty response since template was sent
                        return Response(content=str(resp), media_type="text/xml")
                        
                    except Exception as template_error:
                        print(f"❌ Failed to send human takeover template: {template_error}")
                        error_msg = "Sorry, I couldn't connect you right now. Please try again."
                        await store_message(user_number, "agent", error_msg, "ai")
                        resp.message().body(error_msg)
                        set_pending_confirmation(user_number, False)
                        return Response(content=str(resp), media_type="text/xml")
                else:
                    # User said no - continue with AI and DON'T check for human request again
                    print(f"❌ User declined human connection: {user_number}")
                    set_pending_confirmation(user_number, False)
                    
                    # Check if user included a question in their "no" response
                    # e.g., "No, I need to know about company more"
                    if len(user_message.split()) > 3:  # More than just "no" or "nope"
                        # User included additional text, process it as a question
                        print(f"📝 User declined and asked a question: {user_message}")
                        
                        reply = await ask_chatgpt(user_number, user_message)
                        await store_message(user_number, "agent", reply, "ai")
                        resp.message().body(reply)
                    else:
                        # Just a simple "no"
                        confirmation_msg = "No problem! I'll continue helping you. What can I assist you with?"
                        await store_message(user_number, "agent", confirmation_msg, "ai")
                        resp.message().body(confirmation_msg)
                    
                    return Response(content=str(resp), media_type="text/xml")
            else:
                # Not a clear yes/no - treat as a new question, clear pending state, and process normally
                print(f"⚠️ User sent different message while pending confirmation, clearing state: {user_message}")
                set_pending_confirmation(user_number, False)
                # Continue to normal AI processing (don't check for human request again)
                # This prevents false triggers when user asks new questions after saying "no"
        
        # Only check for human request if NOT coming from a pending confirmation state
        elif is_human_request(user_message):
            print(f"👤 User requested human agent: {user_number}")
            
            # Ask for confirmation
            set_pending_confirmation(user_number, True)
            confirmation_msg = "Would you like me to connect you to a support executive?"
            
            await store_message(user_number, "agent", confirmation_msg, "ai")
            resp.message().body(confirmation_msg)
            
            print(f"❓ Confirmation request sent to {user_number}")
            return Response(content=str(resp), media_type="text/xml")
        
        reply = await ask_chatgpt(user_number, user_message)
        print(f"DEBUG: AI Reply to {user_number} -> {reply}")

        # Store AI message to permanent chat history
        await store_message(user_number, "agent", reply, "ai")

        resp.message().body(reply)

    except Exception as e:
        print("ERROR:", e)
        error_msg = "I'm sorry, I hit a snag. Try again?"
        
        # Store error message
        await store_message(user_number, "agent", error_msg, "ai")
        
        resp.message().body(error_msg)

    webhook_time = time.time() - webhook_start
    print(f"⏱️ Total webhook processing time: {webhook_time:.2f} seconds\n")
    
    return Response(
        content=str(resp),
        media_type="text/xml"
    )
    


# -------- DASHBOARD API ENDPOINTS --------

@app.get("/conversations")
async def get_conversations():
    """Get all active conversations"""
    conversations = []
    
    if mongo_client is not None:
        # Get from MongoDB
        sessions = sessions_collection.find({})
        for session in sessions:
            phone_number = session["phone_number"]
            
            # Get last message from chats
            last_chat = chats_collection.find_one(
                {"phone_number": phone_number},
                sort=[("timestamp", -1)]
            )
            
            conversations.append({
                "phone_number": phone_number,
                "human_takeover": session.get("human_takeover", False),
                "last_message": last_chat["content"] if last_chat else "",
                "last_message_time": last_chat["timestamp"].isoformat() if last_chat else ""
            })
    else:
        # Fallback to in-memory storage
        for phone_number in MESSAGE_STORE.keys():
            messages = MESSAGE_STORE[phone_number]
            last_message = messages[-1] if messages else None
            
            conversations.append({
                "phone_number": phone_number,
                "human_takeover": HUMAN_TAKEOVER.get(phone_number, False),
                "last_message": last_message["content"] if last_message else "",
                "last_message_time": last_message["timestamp"] if last_message else ""
            })
    
    return conversations

@app.get("/messages/{phone_number}")
async def get_messages(phone_number: str):
    """Get all messages for a specific conversation"""
    # Handle URL encoding
    phone_number = phone_number.replace("%3A", ":")
    
    if mongo_client is not None:
        # Get from MongoDB
        messages = get_chat_history(phone_number, limit=100)
        result = [{
            "sender": msg["sender"],
            "content": msg["content"],
            "timestamp": msg["timestamp"].isoformat(),
            "type": msg["type"]
        } for msg in messages]
        
        if result:
            print(f"📤 Sending {len(result)} messages for {phone_number}")
            print(f"   First message timestamp: {result[0]['timestamp']}")
            print(f"   Last message timestamp: {result[-1]['timestamp']}")
        
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
    
    update_human_takeover(phone_number, True)
    print(f"🧑 Human takeover activated for {phone_number}")
    
    # Automatically send template message when taking over
    try:
        twilio_message = twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
            to=phone_number,
            content_sid=HUMAN_TAKEOVER_SID  # Use human takeover template
        )
        
        # Store template message
        await store_message(phone_number, "agent", "Human agent has joined the conversation (template sent)", "human")
        
        print(f"✅ Template sent automatically on takeover, SID: {twilio_message.sid}")
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
    
    update_human_takeover(phone_number, False)
    print(f"🤖 AI mode restored for {phone_number}")
    
    return {"success": True, "message": "Released to AI"}

@app.post("/send-message")
async def send_message(request: Request):
    """Human agent sends a message"""
    data = await request.json()
    phone_number = data.get("phone_number")
    message = data.get("message")
    use_template = data.get("use_template", False)
    
    if not phone_number or not message:
        raise HTTPException(status_code=400, detail="phone_number and message are required")
    
    # Check if human has taken over
    if not get_human_takeover_status(phone_number):
        raise HTTPException(status_code=403, detail="Must take over conversation first")
    
    try:
        if use_template:
            # Use approved template for messages outside 24-hour window
            twilio_message = twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=phone_number,
                content_sid=CONTENT_SID
            )
        else:
            # Try to send free-form message (only works within 24-hour window)
            twilio_message = twilio_client.messages.create(
                from_=f"whatsapp:{TWILIO_WHATSAPP_NUMBER}",
                to=phone_number,
                body=message
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
