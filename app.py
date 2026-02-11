import os
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from openai import OpenAI
from collections import deque
from pydantic import BaseModel

# ---------------- CONFIG ----------------
MAX_HISTORY = 6
MODEL_NAME = "gpt-4o-mini"
# ----------------------------------------

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- PROFESSIONAL CREDENTIALS ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # Your registered business number
CONTENT_SID = os.getenv("CONTENT_SID") # The 'HX...' ID from Content Editor

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

# -------- IMAGE REQUEST DETECTION --------
def is_image_request(text: str) -> bool:
    return any(
        word in text.lower()
        for word in ["image", "images", "photo", "photos", "picture", "show"]
    )

# -------- CHATGPT WITH PROMPT-ONLY KNOWLEDGE --------
def ask_chatgpt(user_number: str, user_message: str) -> str:
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

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.2
    )

    reply = response.choices[0].message.content.strip()

    USER_HISTORY[user_number].append(
        {"role": "assistant", "content": reply}
    )

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

# -------- WHATSAPP WEBHOOK --------
@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    user_message = form.get("Body", "").strip()
    user_number = form.get("From", "")

    resp = MessagingResponse()

    try:
        reply = ask_chatgpt(user_number, user_message)
        print(f"DEBUG: AI Reply to {user_number} -> {reply}") # Check if this prints!

        # Use .body() to ensure Twilio escapes special characters correctly
        resp.message().body(reply)

    except Exception as e:
        print("ERROR:", e)
        resp.message().body("I'm sorry, I hit a snag. Try again?")

    return Response(
        content=str(resp),
        media_type="text/xml"
    )
    
