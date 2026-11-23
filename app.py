import os
import json
import asyncio
from datetime import datetime
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks
from rapidfuzz import process, fuzz

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "banpur_toto_2026")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

with open("landmarks.json", "r", encoding="utf-8") as f:
    DATA = json.load(f)

# --- IN-MEMORY DATABASE (Swap with Supabase/PostgreSQL in production) ---
DRIVERS = {
    "919000000001": {
        "name": "Suman Das",
        "toto_info": "Blue-White Toto (WB-51-1024)",
        "online": False,
        "stand_id": None,
        "joined_queue_at": None,
        "active_ride_id": None
    },
    "919000000002": {
        "name": "Raju Mondal",
        "toto_info": "Red-Black Toto (WB-51-3319)",
        "online": False,
        "stand_id": None,
        "joined_queue_at": None,
        "active_ride_id": None
    }
}

PASSENGER_SESSIONS = {}
RIDES = {}
RIDE_COUNTER = 100

# --- WHATSAPP CLOUD API HELPERS ---
async def send_whatsapp_message(to: str, body_text: str, buttons: list = None):
    """Sends plain text or up to 3 interactive reply buttons."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

    if buttons:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                        for b in buttons[:3]
                    ]
                }
            }
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body_text}
        }

    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)

async def send_whatsapp_list(to: str, body_text: str, button_label: str, section_title: str, rows: list):
    """Sends a scrollable WhatsApp interactive list menu (supports up to 10 items)."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

    formatted_rows = [
        {
            "id": r["id"],
            "title": r["title"][:24],
            "description": r.get("description", "")[:72]
        }
        for r in rows[:10]
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_label[:20],
                "sections": [
                    {
                        "title": section_title[:24],
                        "rows": formatted_rows
                    }
                ]
            }
        }
    }

    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)

async def transcribe_voice_note(media_id: str) -> str:
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    async with httpx.AsyncClient() as client:
        meta_res = await client.get(f"https://graph.facebook.com/v20.0/{media_id}", headers=headers)
        media_url = meta_res.json().get("url")
        audio_bytes = (await client.get(media_url, headers=headers)).content

        files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
        data = {
            "model": "whisper-large-v3",
            "prompt": "Banpur, Bakultala, Station, Bazar, Hospital, High School, Toto, রিজার্ভ, স্টেশন, বকুলতলা, বাজার, হাসপাতাল, স্কুল"
        }
        stt_res = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files=files,
            data=data
        )
        return stt_res.json().get("text", "")

# --- LANDMARK HELPERS ---
def match_landmarks(raw_text: str):
    text = raw_text.lower()
    separators = [" to ", " থেকে ", " theke ", " -> ", " - "]
    parts = [text]
    for sep in separators:
        if sep in text:
            parts = text.split(sep, 1)
            break

    alias_map = {alias: lm["id"] for lm in DATA["landmarks"] for alias in lm["aliases"]}

    def find_best(snippet: str):
        match = process.extractOne(snippet.strip(), alias_map.keys(), scorer=fuzz.partial_ratio)
        return alias_map[match[0]] if (match and match[1] >= 75) else None

    pickup_id = find_best(parts[0]) if len(parts) >= 1 else None
    drop_id = find_best(parts[1]) if len(parts) == 2 else None
    ride_type = "full" if any(w in text for w in ["full", "reserve", "রিজার্ভ", "পুরো"]) else "share"

    seats = 1
    for num in ["2", "3", "4", "২", "৩", "৪"]:
        if num in text:
            seats = int(num.translate(str.maketrans("২৩৪", "234")))

    return pickup_id, drop_id, ride_type, seats

def get_landmark_name(lm_id: str) -> str:
    for lm in DATA["landmarks"]:
        if lm["id"] == lm_id:
            return f"{lm['name_bn']} ({lm['name_en']})"
    return lm_id

def build_landmark_rows(prefix: str, exclude_id: str = None):
    rows = []
    for lm in DATA["landmarks"]:
        if lm["id"] == exclude_id:
            continue
        rows.append({
            "id": f"{prefix}{lm['id']}",
            "title": lm["name_bn"],
            "description": lm["name_en"]
        })
    return rows

async def send_fare_confirmation(sender: str, pickup_id: str, drop_id: str, ride_type: str = "share", seats: int = 1):
    route_key = f"{pickup_id}->{drop_id}"
    route = DATA["routes"].get(route_key, {"share_fare": 15, "full_fare": 60})
    fare = route["full_fare"] if ride_type == "full" else route["share_fare"] * seats

    PASSENGER_SESSIONS[sender] = {
        "pickup": pickup_id,
        "drop": drop_id,
        "type": ride_type,
        "seats": seats,
        "fare": fare
    }

    await send_whatsapp_message(
        sender,
        f"বুকিং তথ্য:\n"
        f"• উঠবেন: {get_landmark_name(pickup_id)}\n"
        f"• নামবেন: {get_landmark_name(drop_id)}\n"
        f"• ধরন: {'রিজার্ভ' if ride_type == 'full' else f'শেয়ার ({seats} সিট)'}\n"
        f"• ভাড়া: ₹{fare}",
        [
            {"id": "CONFIRM_RIDE", "title": f"✅ Confirm ₹{fare}"},
            {"id": "TOGGLE_FULL", "title": "🛺 Reserve Full"},
            {"id": "CANCEL_NEW", "title": "❌ Cancel"}
        ]
    )

# --- DRIVER QUEUE & DISPATCH ENGINE ---
def get_next_driver_at_stand(stand_id: str, excluded_drivers: list):
    eligible = [
        (phone, d) for phone, d in DRIVERS.items()
        if d["online"] and d["stand_id"] == stand_id and not d["active_ride_id"] and phone not in excluded_drivers
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[1]["joined_queue_at"])
    return eligible[0][0]

async def dispatch_to_next_driver(ride_id: str):
    ride = RIDES.get(ride_id)
    if not ride or ride["status"] != "SEARCHING":
        return

    driver_phone = get_next_driver_at_stand(ride["pickup"], ride["rejected_by"])
    if not driver_phone:
        ride["status"] = "NO_DRIVER"
        await send_whatsapp_message(
            ride["passenger"],
            "দুঃখিত, এই মুহূর্তে এই স্ট্যান্ডে কোনো খালি টোটো নেই। কিছুক্ষণ পর আবার চেষ্টা করুন।",
            [{"id": "OPEN_PICKUP_MENU", "title": "🔄 Try Again"}]
        )
        return

    ride["offered_to"] = driver_phone
    p_name = get_landmark_name(ride["pickup"])
    d_name = get_landmark_name(ride["drop"])

    await send_whatsapp_message(
        driver_phone,
        f"🔔 নতুন ভাড়া! (Ride #{ride_id})\n"
        f"• উঠবে: {p_name}\n"
        f"• নামবে: {d_name}\n"
        f"• ধরন: {ride['type'].upper()} ({ride['seats']} সিট)\n"
        f"• ভাড়া: ₹{ride['fare']}",
        [
            {"id": f"ACCEPT_{ride_id}", "title": f"✅ Accept ₹{ride['fare']}"},
            {"id": f"SKIP_{ride_id}", "title": "⏭️ Skip"}
        ]
    )

    await asyncio.sleep(25)
    if RIDES.get(ride_id, {}).get("status") == "SEARCHING" and RIDES[ride_id].get("offered_to") == driver_phone:
        RIDES[ride_id]["rejected_by"].append(driver_phone)
        await send_whatsapp_message(driver_phone, "⏰ সময় শেষ! ভাড়াটি পরের টোটোকে পাঠানো হয়েছে।")
        await dispatch_to_next_driver(ride_id)

# --- DRIVER HANDLER ---
async def handle_driver_message(driver_phone: str, message: dict):
    driver = DRIVERS[driver_phone]
    msg_type = message.get("type")

    if msg_type == "interactive":
        interactive_type = message["interactive"]["type"]
        selected_id = (
            message["interactive"]["list_reply"]["id"]
            if interactive_type == "list_reply"
            else message["interactive"]["button_reply"]["id"]
        )

        # Driver selects Stand from scrollable list
        if selected_id.startswith("STAND_"):
            stand_id = selected_id.replace("STAND_", "")
            driver["online"] = True
            driver["stand_id"] = stand_id
            driver["joined_queue_at"] = datetime.utcnow().isoformat()
            await send_whatsapp_message(
                driver_phone,
                f"✅ আপনি এখন অনলাইন!\nস্ট্যান্ড: {get_landmark_name(stand_id)}\nভাড়া এলে এখানেই মেসেজ পাবেন।",
                [{"id": "DRIVER_OFF", "title": "🔴 Go Offline"}]
            )
            return

        if selected_id == "DRIVER_OFF":
            driver["online"] = False
            driver["stand_id"] = None
            await send_whatsapp_message(driver_phone, "🔴 আপনি অফলাইন হয়েছেন। আবার ডিউটি শুরু করতে 'duty' লিখুন।")
            return

        if selected_id.startswith("ACCEPT_"):
            ride_id = selected_id.replace("ACCEPT_", "")
            ride = RIDES.get(ride_id)
            if not ride or ride["status"] != "SEARCHING":
                await send_whatsapp_message(driver_phone, "এই বুকিংটি আর উপলব্ধ নেই।")
                return

            ride["status"] = "ASSIGNED"
            ride["driver"] = driver_phone
            driver["active_ride_id"] = ride_id

            await send_whatsapp_message(
                driver_phone,
                f"🛺 বুকিং কনফার্ম হয়েছে! (#{ride_id})\n"
                f"• যাত্রী ফোন: +{ride['passenger']}\n"
                f"• উঠবে: {get_landmark_name(ride['pickup'])}\n"
                f"• নামবে: {get_landmark_name(ride['drop'])}\n"
                f"• ভাড়া: ₹{ride['fare']}",
                [{"id": f"DONE_{ride_id}", "title": "🏁 Trip Done"}]
            )

            await send_whatsapp_message(
                ride["passenger"],
                f"✅ আপনার টোটো বুক হয়েছে!\n"
                f"• ড্রাইভার: {driver['name']}\n"
                f"• গাড়ি: {driver['toto_info']}\n"
                f"• ড্রাইভার ফোন: +{driver_phone}\n"
                f"• ভাড়া: ₹{ride['fare']} (নগদ বা UPI দেবেন)",
                [{"id": f"CANCEL_{ride_id}", "title": "❌ Cancel Ride"}]
            )
            return

        if selected_id.startswith("SKIP_"):
            ride_id = selected_id.replace("SKIP_", "")
            ride = RIDES.get(ride_id)
            if ride and ride["status"] == "SEARCHING":
                ride["rejected_by"].append(driver_phone)
                await send_whatsapp_message(driver_phone, "⏭️ স্কিপ করা হয়েছে।")
                await dispatch_to_next_driver(ride_id)
            return

        if selected_id.startswith("DONE_"):
            ride_id = selected_id.replace("DONE_", "")
            ride = RIDES.get(ride_id)
            if ride:
                ride["status"] = "COMPLETED"
                driver["active_ride_id"] = None
                driver["stand_id"] = ride["drop"]
                driver["joined_queue_at"] = datetime.utcnow().isoformat()

                await send_whatsapp_message(
                    driver_phone,
                    f"🏁 ট্রিপ সম্পন্ন! আপনি এখন {get_landmark_name(ride['drop'])} স্ট্যান্ডের লাইনে যুক্ত হয়েছেন।",
                    [{"id": "DRIVER_OFF", "title": "🔴 Go Offline"}]
                )
                await send_whatsapp_message(
                    ride["passenger"],
                    f"🙏 যাত্রা শুভ হোক! ভাড়া ₹{ride['fare']} পরিশোধের জন্য ধন্যবাদ। আবার টোটো লাগলে মেসেজ করুন।"
                )
            return

    if msg_type == "text":
        txt = message["text"]["body"].strip().lower()
        if txt in ["off", "offline", "অফ", "অফলাইন"]:
            driver["online"] = False
            driver["stand_id"] = None
            await send_whatsapp_message(driver_phone, "🔴 আপনি অফলাইন হয়েছেন।")
        else:
            stand_rows = build_landmark_rows("STAND_")
            await send_whatsapp_list(
                to=driver_phone,
                body_text=f"নমস্কার {driver['name']}! 🛺 নিচের তালিকা থেকে আপনার বর্তমান স্ট্যান্ড বেছে নিন:",
                button_label="📍 Choose Stand",
                section_title="Banpur Toto Stands",
                rows=stand_rows
            )

# --- PASSENGER HANDLER ---
async def handle_passenger_message(sender: str, message: dict):
    global RIDE_COUNTER
    msg_type = message.get("type")

    if msg_type == "interactive":
        interactive_type = message["interactive"]["type"]
        selected_id = (
            message["interactive"]["list_reply"]["id"]
            if interactive_type == "list_reply"
            else message["interactive"]["button_reply"]["id"]
        )

        # Step 1 from Menu: Passenger picked Pickup Stand -> Show Drop Stand List
        if selected_id.startswith("PICKUP_"):
            pickup_id = selected_id.replace("PICKUP_", "")
            PASSENGER_SESSIONS[sender] = {"pickup": pickup_id}
            drop_rows = build_landmark_rows("DROP_", exclude_id=pickup_id)
            await send_whatsapp_list(
                to=sender,
                body_text=f"উঠবেন: *{get_landmark_name(pickup_id)}*\nএবার কোথায় নামবেন বেছে নিন:",
                button_label="🏁 Select Drop",
                section_title="Drop Landmarks",
                rows=drop_rows
            )
            return

        # Step 2 from Menu: Passenger picked Drop Stand -> Show Fare Confirmation
        if selected_id.startswith("DROP_"):
            drop_id = selected_id.replace("DROP_", "")
            pickup_id = PASSENGER_SESSIONS.get(sender, {}).get("pickup")
            if not pickup_id:
                return
            await send_fare_confirmation(sender, pickup_id, drop_id)
            return

        if selected_id == "OPEN_PICKUP_MENU":
            pickup_rows = build_landmark_rows("PICKUP_")
            await send_whatsapp_list(
                to=sender,
                body_text="কোথা থেকে উঠবেন বেছে নিন:",
                button_label="📍 Select Pickup",
                section_title="Pickup Landmarks",
                rows=pickup_rows
            )
            return

        if selected_id == "CONFIRM_RIDE":
            session = PASSENGER_SESSIONS.get(sender)
            if not session or "drop" not in session:
                return
            RIDE_COUNTER += 1
            ride_id = str(RIDE_COUNTER)
            RIDES[ride_id] = {
                "id": ride_id,
                "passenger": sender,
                "pickup": session["pickup"],
                "drop": session["drop"],
                "type": session["type"],
                "seats": session["seats"],
                "fare": session["fare"],
                "status": "SEARCHING",
                "rejected_by": [],
                "offered_to": None,
                "driver": None
            }
            await send_whatsapp_message(
                sender,
                "🔍 কাছের স্ট্যান্ডে টোটো খোঁজা হচ্ছে... ২০-৩০ সেকেন্ড অপেক্ষা করুন।",
                [{"id": f"CANCEL_{ride_id}", "title": "❌ Cancel"}]
            )
            await dispatch_to_next_driver(ride_id)
            return

        if selected_id == "TOGGLE_FULL":
            session = PASSENGER_SESSIONS.get(sender)
            if session and "drop" in session:
                route = DATA["routes"].get(f"{session['pickup']}->{session['drop']}", {"full_fare": 60})
                session["type"] = "full"
                session["fare"] = route["full_fare"]
                await send_whatsapp_message(
                    sender,
                    f"রিজার্ভ টোটো ভাড়া: ₹{session['fare']}। কনফার্ম করবেন?",
                    [{"id": "CONFIRM_RIDE", "title": f"✅ Confirm ₹{session['fare']}"}, {"id": "CANCEL_NEW", "title": "❌ Cancel"}]
                )
            return

        if selected_id.startswith("CANCEL_"):
            ride_id = selected_id.replace("CANCEL_", "")
            ride = RIDES.get(ride_id)
            if ride and ride["status"] in ["SEARCHING", "ASSIGNED"]:
                ride["status"] = "CANCELLED"
                if ride.get("driver"):
                    d_phone = ride["driver"]
                    DRIVERS[d_phone]["active_ride_id"] = None
                    await send_whatsapp_message(d_phone, f"❌ যাত্রী বুকিং #{ride_id} বাতিল করেছেন। আপনি আবার স্ট্যান্ড লাইনে আছেন।")
                await send_whatsapp_message(sender, "❌ আপনার বুকিং বাতিল করা হয়েছে।")
            else:
                await send_whatsapp_message(sender, "❌ বুকিং বাতিল করা হয়েছে।")
            return

    # Process Voice Note or Text Landmark
    if msg_type == "audio":
        raw_text = await transcribe_voice_note(message["audio"]["id"])
    elif msg_type == "text":
        raw_text = message["text"]["body"]
    else:
        return

    pickup_id, drop_id, ride_type, seats = match_landmarks(raw_text)
    if not pickup_id or not drop_id or pickup_id == drop_id:
        pickup_rows = build_landmark_rows("PICKUP_")
        await send_whatsapp_list(
            to=sender,
            body_text="নমস্কার! 🛺 ভয়েস নোট পাঠান, জায়গার নাম লিখুন (যেমন: *বকুলতলা থেকে স্টেশন*), অথবা নিচের তালিকা থেকে জায়গা বেছে নিন:",
            button_label="📍 Select Pickup",
            section_title="Pickup Landmarks",
            rows=pickup_rows
        )
        return

    await send_fare_confirmation(sender, pickup_id, drop_id, ride_type, seats)

# --- WEBHOOK ENDPOINTS ---
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(status_code=403)

@app.post("/webhook")
async def incoming_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            msg = value["messages"][0]
            sender = msg["from"]
            if sender in DRIVERS:
                background_tasks.add_task(handle_driver_message, sender, msg)
            else:
                background_tasks.add_task(handle_passenger_message, sender, msg)
    except (KeyError, IndexError):
        pass
    return {"status": "ok"}
