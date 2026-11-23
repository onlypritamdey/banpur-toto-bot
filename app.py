import asyncio
import json
import os
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from rapidfuzz import fuzz, process

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1484657768057698")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "banpur_toto_2026")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

GRAPH_API_VERSION = "v27.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

with open("landmarks.json", "r", encoding="utf-8") as f:
    DATA = json.load(f)

LANDMARKS: List[Dict[str, Any]] = DATA.get("landmarks", [])
STANDS: List[Dict[str, Any]] = DATA.get("stands", [])
FARES: Dict[str, Any] = DATA.get("fares", {})
DEFAULT_FARE = int(DATA.get("default_fare", 10))
FULL_RESERVE_MULTIPLIER = int(DATA.get("full_reserve_multiplier", 4))

# --- IN-MEMORY STORES (Swap with Supabase later for persistence) ---
DRIVERS: Dict[str, Dict[str, Any]] = {
    "919000000001": {
        "name": "Suman Das",
        "toto_info": "Blue-White Toto (WB-51-1024)",
        "online": False,
        "stand_id": None,
        "joined_queue_at": None,
        "active_ride_id": None,
    },
    "919000000002": {
        "name": "Raju Mondal",
        "toto_info": "Green Toto (WB-51-2048)",
        "online": False,
        "stand_id": None,
        "joined_queue_at": None,
        "active_ride_id": None,
    },
}

RIDERS: Dict[str, Dict[str, Any]] = {}
ONBOARDING_STATE: Dict[str, Dict[str, Any]] = {}
RIDER_BOOKINGS: Dict[str, Dict[str, Any]] = {}
RIDES: Dict[str, Dict[str, Any]] = {}
STAND_QUEUES: Dict[str, List[str]] = {}
SEEN_MESSAGE_IDS: set[str] = set()
RIDE_COUNTER = 100


def get_stand_ids() -> List[str]:
    if STANDS:
        return [str(s.get("id", s.get("name"))) for s in STANDS]
    return ["banpur_station", "bakultala"]


for sid in get_stand_ids():
    STAND_QUEUES.setdefault(sid, [])


# --- WHATSAPP API HELPERS ---
async def send_whatsapp_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("Missing WHATSAPP_TOKEN or PHONE_NUMBER_ID in environment.")
        return None
    url = f"{GRAPH_BASE_URL}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            print(f"WhatsApp send error ({resp.status_code}): {resp.text}")
            return None
        return resp.json()


async def send_text(to: str, body: str) -> None:
    await send_whatsapp_payload(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
    )


async def send_buttons(to: str, body_text: str, buttons: List[Dict[str, str]]) -> None:
    # WhatsApp reply buttons allow up to 3 buttons, max 20 chars per title
    formatted_buttons = [
        {
            "type": "reply",
            "reply": {
                "id": btn["id"][:256],
                "title": btn["title"][:20],
            },
        }
        for btn in buttons[:3]
    ]
    await send_whatsapp_payload(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": formatted_buttons},
            },
        }
    )


async def send_list_menu(
    to: str,
    body_text: str,
    button_label: str,
    section_title: str,
    rows: List[Dict[str, str]],
) -> None:
    # WhatsApp list messages allow up to 10 rows per section
    formatted_rows = [
        {
            "id": row["id"][:200],
            "title": row["title"][:24],
            **({"description": row["description"][:72]} if row.get("description") else {}),
        }
        for row in rows[:10]
    ]
    await send_whatsapp_payload(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
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
                            "rows": formatted_rows,
                        }
                    ],
                },
            },
        }
    )


# --- LANDMARK, FARE & QUEUE HELPERS ---
def landmark_display(lm: Dict[str, Any]) -> str:
    bn = lm.get("name_bn") or lm.get("bn") or lm.get("name") or ""
    en = lm.get("name_en") or lm.get("en") or lm.get("name") or ""
    if bn and en and bn != en:
        return f"{bn} ({en})"
    return bn or en


def find_landmark_by_id(lm_id: str) -> Optional[Dict[str, Any]]:
    for lm in LANDMARKS:
        if str(lm.get("id")) == lm_id:
            return lm
    return None


def match_landmark_text(query: str) -> Optional[Dict[str, Any]]:
    if not query or not LANDMARKS:
        return None
    choices: Dict[str, Dict[str, Any]] = {}
    for lm in LANDMARKS:
        for key in ("name_bn", "name_en", "name", "bn", "en"):
            val = lm.get(key)
            if val:
                choices[str(val)] = lm
        for alias in lm.get("aliases", []) or []:
            choices[str(alias)] = lm
    if not choices:
        return None
    best = process.extractOne(query.strip(), list(choices.keys()), scorer=fuzz.WRatio)
    if best and best[1] >= 65:
        return choices[best[0]]
    return None


def calculate_fare(pickup_id: str, drop_id: str, reserve_full: bool = False) -> int:
    key_direct = f"{pickup_id}__{drop_id}"
    key_reverse = f"{drop_id}__{pickup_id}"
    base = DEFAULT_FARE
    if isinstance(FARES, dict):
        if key_direct in FARES:
            base = int(FARES[key_direct])
        elif key_reverse in FARES:
            base = int(FARES[key_reverse])
        else:
            p_lm = find_landmark_by_id(pickup_id) or {}
            d_lm = find_landmark_by_id(drop_id) or {}
            zone_diff = abs(int(p_lm.get("zone", 1)) - int(d_lm.get("zone", 1)))
            base = DEFAULT_FARE + (zone_diff * 5)
    return base * FULL_RESERVE_MULTIPLIER if reserve_full else base


def nearest_stand_for_landmark(lm: Optional[Dict[str, Any]]) -> str:
    if lm and lm.get("stand_id") and str(lm["stand_id"]) in STAND_QUEUES:
        return str(lm["stand_id"])
    for sid, queue in STAND_QUEUES.items():
        if queue:
            return sid
    return get_stand_ids()[0]


def remove_driver_from_queues(driver_phone: str) -> None:
    for q in STAND_QUEUES.values():
        while driver_phone in q:
            q.remove(driver_phone)


def pop_next_driver(stand_id: str) -> Optional[str]:
    search_order = [stand_id] + [s for s in STAND_QUEUES.keys() if s != stand_id]
    for sid in search_order:
        q = STAND_QUEUES.get(sid, [])
        while q:
            cand = q.pop(0)
            drv = DRIVERS.get(cand)
            if drv and drv.get("online") and not drv.get("active_ride_id"):
                return cand
    return None


async def send_role_menu(to: str) -> None:
    await send_buttons(
        to,
        "নমস্কার! 🛺 Banpur Toto-তে স্বাগতম।\nআপনি কীভাবে যুক্ত হতে চান?",
        [
            {"id": "role_rider", "title": "Book a Toto"},
            {"id": "role_driver", "title": "Driver Signup"},
        ],
    )


async def send_pickup_menu(to: str) -> None:
    rows = [
        {
            "id": f"pickup_{lm.get('id')}",
            "title": (lm.get("name_bn") or lm.get("name") or str(lm.get("id")))[:24],
            "description": (lm.get("name_en") or "")[:72],
        }
        for lm in LANDMARKS[:10]
    ]
    await send_list_menu(
        to,
        "নমস্কার! 🛺 জায়গার নাম লিখুন (যেমন: বকুলতলা থেকে স্টেশন), অথবা নিচের তালিকা থেকে জায়গা বেছে নিন:",
        "Select Pickup",
        "Banpur Pickups",
        rows,
    )


async def send_drop_menu(to: str, pickup_lm: Dict[str, Any]) -> None:
    rows = [
        {
            "id": f"drop_{lm.get('id')}",
            "title": (lm.get("name_bn") or lm.get("name") or str(lm.get("id")))[:24],
            "description": (lm.get("name_en") or "")[:72],
        }
        for lm in LANDMARKS[:10]
        if str(lm.get("id")) != str(pickup_lm.get("id"))
    ]
    await send_list_menu(
        to,
        f"উঠবেন: {landmark_display(pickup_lm)}\nএবার কোথায় নামবেন বেছে নিন:",
        "Select Drop",
        "Banpur Drops",
        rows,
    )


async def send_booking_summary(to: str, booking: Dict[str, Any]) -> None:
    pickup = find_landmark_by_id(booking["pickup_id"]) or {"name": booking["pickup_id"]}
    drop = find_landmark_by_id(booking["drop_id"]) or {"name": booking["drop_id"]}
    fare = calculate_fare(booking["pickup_id"], booking["drop_id"], reserve_full=False)
    booking["fare"] = fare
    summary = (
        "বুকিং তথ্য:\n"
        f"• উঠবেন: {landmark_display(pickup)}\n"
        f"• নামবেন: {landmark_display(drop)}\n"
        "• ধরন: শেয়ার (১ সিট)\n"
        f"• ভাড়া: ₹{fare}"
    )
    await send_buttons(
        to,
        summary,
        [
            {"id": "confirm_share", "title": f"Confirm ₹{fare}"},
            {"id": "confirm_full", "title": "Reserve Full"},
            {"id": "cancel_booking", "title": "Cancel"},
        ],
    )


async def send_stand_menu(to: str) -> None:
    rows: List[Dict[str, str]] = []
    if STANDS:
        for st in STANDS[:10]:
            sid = str(st.get("id", st.get("name")))
            title = (st.get("name_bn") or st.get("name") or sid)[:24]
            desc = (st.get("name_en") or f"Queue: {len(STAND_QUEUES.get(sid, []))}")[:72]
            rows.append({"id": f"stand_{sid}", "title": title, "description": desc})
    else:
        for sid in get_stand_ids()[:10]:
            rows.append({"id": f"stand_{sid}", "title": sid[:24], "description": "Banpur Stand"})
    await send_list_menu(
        to,
        "ডিউটিতে যেতে কোন স্ট্যান্ডে আছেন বেছে নিন:",
        "Choose Stand",
        "Banpur Stands",
        rows,
    )


# --- VOICE TRANSCRIPTION HELPER (OPTIONAL GROQ) ---
async def transcribe_voice_note(media_id: str) -> Optional[str]:
    if not GROQ_API_KEY or not WHATSAPP_TOKEN:
        return None
    async with httpx.AsyncClient(timeout=25.0) as client:
        meta_resp = await client.get(
            f"{GRAPH_BASE_URL}/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        )
        if meta_resp.status_code >= 400:
            return None
        media_url = meta_resp.json().get("url")
        if not media_url:
            return None

        audio_resp = await client.get(
            media_url,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        )
        if audio_resp.status_code >= 400:
            return None

        groq_resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio_resp.content, "audio/ogg")},
            data={"model": "whisper-large-v3-turbo", "language": "bn"},
        )
        if groq_resp.status_code >= 400:
            return None
        return groq_resp.json().get("text", "").strip()


# --- DISPATCH & 4-DIGIT PIN LOGIC ---
async def dispatch_ride(ride_id: str) -> None:
    global RIDE_COUNTER
    ride = RIDES.get(ride_id)
    if not ride or ride["status"] != "SEARCHING":
        return

    rider_phone = ride["rider_phone"]
    stand_id = ride["stand_id"]
    driver_phone = pop_next_driver(stand_id)

    if not driver_phone:
        await send_buttons(
            rider_phone,
            "দুঃখিত, এই মুহূর্তে এই স্ট্যান্ডে কোনো খালি টোটো নেই। কিছুক্ষণ পর আবার চেষ্টা করুন।",
            [
                {"id": f"retry_ride_{ride_id}", "title": "Try Again"},
                {"id": "cancel_booking", "title": "Cancel"},
            ],
        )
        return

    driver = DRIVERS[driver_phone]
    pin = f"{random.randint(1000, 9999)}"
    ride["driver_phone"] = driver_phone
    ride["pin"] = pin
    ride["status"] = "ASSIGNED"
    driver["active_ride_id"] = ride_id

    pickup_text = landmark_display(find_landmark_by_id(ride["pickup_id"]) or {"name": ride["pickup_id"]})
    drop_text = landmark_display(find_landmark_by_id(ride["drop_id"]) or {"name": ride["drop_id"]})
    rider_name = RIDERS.get(rider_phone, {}).get("name") or f"+{rider_phone}"

    await send_text(
        rider_phone,
        "🛺 টোটো পাওয়া গেছে!\n"
        f"• ড্রাইভার: {driver['name']} ({driver['toto_info']})\n"
        f"• ফোন: +{driver_phone}\n"
        f"• ভাড়া: ₹{ride['fare']}\n"
        f"• আপনার রাইড PIN: *{pin}*\n"
        "(টোটোতে ওঠার সময় ড্রাইভারকে এই ৪ সংখ্যার PIN বলুন)",
    )

    await send_text(
        driver_phone,
        "🔔 নতুন টোটো বুকিং!\n"
        f"• যাত্রী: {rider_name} (+{rider_phone})\n"
        f"• উঠবেন: {pickup_text}\n"
        f"• নামবেন: {drop_text}\n"
        f"• ভাড়া: ₹{ride['fare']}\n"
        "যাত্রী টোটোতে উঠলে তাঁর ৪ সংখ্যার PIN লিখে পাঠান:",
    )


async def handle_route_text(sender: str, text: str) -> bool:
    # Supports phrases like "বকুলতলা থেকে স্টেশন" or "Bakultala to Station"
    parts = None
    for sep in [" থেকে ", " to ", " - ", " -> "]:
        if sep in text:
            left, right = text.split(sep, 1)
            parts = (left.strip(), right.strip())
            break
    if not parts:
        return False

    p_lm = match_landmark_text(parts[0])
    d_lm = match_landmark_text(parts[1])
    if p_lm and d_lm and str(p_lm.get("id")) != str(d_lm.get("id")):
        RIDER_BOOKINGS[sender] = {
            "pickup_id": str(p_lm.get("id")),
            "drop_id": str(d_lm.get("id")),
        }
        await send_booking_summary(sender, RIDER_BOOKINGS[sender])
        return True
    return False


# --- WEBHOOK ENDPOINTS ---
@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain", status_code=200)
    return Response(content="Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Dict[str, str]:
    global RIDE_COUNTER
    payload = await request.json()

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = value.get("contacts", [])
            profile_name = ""
            if contacts:
                profile_name = contacts[0].get("profile", {}).get("name", "")

            for msg in value.get("messages", []):
                msg_id = msg.get("id")
                if msg_id:
                    if msg_id in SEEN_MESSAGE_IDS:
                        continue
                    SEEN_MESSAGE_IDS.add(msg_id)

                sender = msg.get("from", "")
                if not sender:
                    continue

                msg_type = msg.get("type", "")
                text = ""
                interactive_id = ""

                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "").strip()
                elif msg_type == "interactive":
                    inter = msg.get("interactive", {})
                    inter_type = inter.get("type", "")
                    if inter_type == "button_reply":
                        interactive_id = inter.get("button_reply", {}).get("id", "")
                    elif inter_type == "list_reply":
                        interactive_id = inter.get("list_reply", {}).get("id", "")
                elif msg_type == "audio":
                    media_id = msg.get("audio", {}).get("id", "")
                    transcribed = await transcribe_voice_note(media_id) if media_id else None
                    if transcribed:
                        text = transcribed
                    else:
                        await send_text(
                            sender,
                            "ভয়েস নোট সিস্টেম এখনো চালু হয়নি। দয়া করে জায়গার নাম লিখুন বা নিচের তালিকা থেকে বেছে নিন।",
                        )
                        if sender in DRIVERS:
                            await send_stand_menu(sender)
                        else:
                            await send_pickup_menu(sender)
                        continue

                text_lower = text.lower()

                # Allow resetting/switching role anytime during testing
                if text_lower in ["register", "role", "signup", "মেনু"]:
                    DRIVERS.pop(sender, None)
                    RIDERS.pop(sender, None)
                    ONBOARDING_STATE.pop(sender, None)
                    RIDER_BOOKINGS.pop(sender, None)
                    await send_role_menu(sender)
                    continue

                # Role selection buttons
                if interactive_id == "role_rider":
                    RIDERS[sender] = {
                        "name": profile_name or "Passenger",
                        "joined_at": datetime.utcnow().isoformat(),
                    }
                    ONBOARDING_STATE.pop(sender, None)
                    await send_pickup_menu(sender)
                    continue

                if interactive_id == "role_driver":
                    RIDERS.pop(sender, None)
                    ONBOARDING_STATE[sender] = {"step": "DRIVER_NAME"}
                    await send_text(sender, "আপনার পুরো নাম লিখুন (যেমন: সুমন দাস):")
                    continue

                # 2-step driver self-registration
                if sender in ONBOARDING_STATE:
                    ob = ONBOARDING_STATE[sender]
                    if ob.get("step") == "DRIVER_NAME":
                        if not text:
                            await send_text(sender, "অনুগ্রহ করে আপনার পুরো নাম লিখুন:")
                            continue
                        ob["name"] = text
                        ob["step"] = "DRIVER_TOTO"
                        await send_text(sender, "আপনার টোটোর নম্বর বা রঙ লিখুন (যেমন: WB-51-1024):")
                        continue
                    if ob.get("step") == "DRIVER_TOTO":
                        if not text:
                            await send_text(sender, "অনুগ্রহ করে আপনার টোটোর নম্বর বা রঙ লিখুন:")
                            continue
                        DRIVERS[sender] = {
                            "name": ob["name"],
                            "toto_info": text,
                            "online": False,
                            "stand_id": None,
                            "joined_queue_at": None,
                            "active_ride_id": None,
                        }
                        ONBOARDING_STATE.pop(sender, None)
                        await send_text(
                            sender,
                            f"✅ রেজিস্ট্রেশন সম্পূর্ণ!\nনাম: {DRIVERS[sender]['name']}\nটোটো: {DRIVERS[sender]['toto_info']}",
                        )
                        await send_stand_menu(sender)
                        continue

                # Brand-new user gets role menu first
                if sender not in DRIVERS and sender not in RIDERS:
                    await send_role_menu(sender)
                    continue

                # --- DRIVER FLOW ---
                if sender in DRIVERS:
                    driver = DRIVERS[sender]
                    active_ride_id = driver.get("active_ride_id")
                    active_ride = RIDES.get(active_ride_id) if active_ride_id else None

                    # Active ride PIN verification & completion
                    if active_ride:
                        if active_ride["status"] == "ASSIGNED" and text.isdigit() and len(text) == 4:
                            if text == active_ride["pin"]:
                                active_ride["status"] = "IN_PROGRESS"
                                await send_text(
                                    sender,
                                    "✅ PIN মিলেছে! রাইড শুরু হয়েছে। গন্তব্যে পৌঁছে নিচের বাটনে চাপুন:",
                                )
                                await send_buttons(
                                    sender,
                                    "যাত্রী গন্তব্যে নেমে গেলে রাইড সম্পন্ন করুন:",
                                    [{"id": f"complete_ride_{active_ride_id}", "title": "Complete Ride"}],
                                )
                                await send_text(
                                    active_ride["rider_phone"],
                                    "✅ রাইড PIN যাচাই হয়েছে এবং যাত্রা শুরু হয়েছে। শুভ যাত্রা!",
                                )
                            else:
                                await send_text(
                                    sender,
                                    "❌ ভুল PIN! যাত্রীর কাছ থেকে সঠিক ৪ সংখ্যার PIN জেনে আবার লিখুন।",
                                )
                            continue

                        if interactive_id == f"complete_ride_{active_ride_id}":
                            active_ride["status"] = "COMPLETED"
                            driver["active_ride_id"] = None
                            stand_id = active_ride["stand_id"]
                            if driver.get("online") and stand_id in STAND_QUEUES:
                                STAND_QUEUES[stand_id].append(sender)
                            await send_text(
                                sender,
                                f"🏁 রাইড সম্পন্ন হয়েছে! আপনি আবার {stand_id} কিউতে যুক্ত হয়েছেন।",
                            )
                            await send_text(
                                active_ride["rider_phone"],
                                "🏁 আপনি গন্তব্যে পৌঁছেছেন। Banpur Toto ব্যবহার করার জন্য ধন্যবাদ!",
                            )
                            continue

                    if text_lower in ["duty", "online", "queue"]:
                        await send_stand_menu(sender)
                        continue

                    if text_lower in ["off", "offline", "stop"]:
                        driver["online"] = False
                        driver["stand_id"] = None
                        remove_driver_from_queues(sender)
                        await send_text(sender, "🛑 আপনি অফলাইনে গেছেন এবং কিউ থেকে বেরিয়ে এসেছেন।")
                        continue

                    if interactive_id.startswith("stand_"):
                        stand_id = interactive_id.replace("stand_", "", 1)
                        STAND_QUEUES.setdefault(stand_id, [])
                        remove_driver_from_queues(sender)
                        driver["online"] = True
                        driver["stand_id"] = stand_id
                        driver["joined_queue_at"] = datetime.utcnow().isoformat()
                        STAND_QUEUES[stand_id].append(sender)
                        pos = STAND_QUEUES[stand_id].index(sender) + 1
                        await send_text(
                            sender,
                            f"🟢 আপনি এখন অনলাইনে আছেন!\nস্ট্যান্ড: {stand_id}\nকিউ পজিশন: #{pos}",
                        )
                        continue

                    await send_buttons(
                        sender,
                        f"ড্রাইভার প্যানেল ({driver['name']}):",
                        [
                            {"id": "stand_banpur_station", "title": "Station Duty"},
                            {"id": "stand_bakultala", "title": "Bakultala Duty"},
                        ],
                    )
                    continue

                # --- RIDER FLOW ---
                RIDERS[sender]["name"] = profile_name or RIDERS[sender].get("name") or "Passenger"

                if interactive_id == "cancel_booking":
                    existing_ride_id = RIDER_BOOKINGS.get(sender, {}).get("ride_id")
                    if existing_ride_id and existing_ride_id in RIDES:
                        RIDES[existing_ride_id]["status"] = "CANCELLED"
                    RIDER_BOOKINGS.pop(sender, None)
                    await send_text(sender, "❌ বুকিং বাতিল করা হয়েছে।")
                    continue

                if interactive_id.startswith("retry_ride_"):
                    old_ride_id = interactive_id.replace("retry_ride_", "", 1)
                    old_ride = RIDES.get(old_ride_id)
                    if old_ride:
                        old_ride["status"] = "SEARCHING"
                        await send_buttons(
                            sender,
                            "🔍 কাছের স্ট্যান্ডে টোটো খোঁজা হচ্ছে... অপেক্ষা করুন।",
                            [{"id": "cancel_booking", "title": "Cancel"}],
                        )
                        asyncio.create_task(dispatch_ride(old_ride_id))
                    continue

                if interactive_id.startswith("pickup_"):
                    pickup_id = interactive_id.replace("pickup_", "", 1)
                    pickup_lm = find_landmark_by_id(pickup_id)
                    if pickup_lm:
                        RIDER_BOOKINGS[sender] = {"pickup_id": str(pickup_lm.get("id"))}
                        await send_drop_menu(sender, pickup_lm)
                    else:
                        await send_pickup_menu(sender)
                    continue

                if interactive_id.startswith("drop_"):
                    drop_id = interactive_id.replace("drop_", "", 1)
                    drop_lm = find_landmark_by_id(drop_id)
                    booking = RIDER_BOOKINGS.get(sender)
                    if not booking or not booking.get("pickup_id"):
                        await send_pickup_menu(sender)
                        continue
                    if not drop_lm or str(drop_lm.get("id")) == str(booking["pickup_id"]):
                        pickup_lm = find_landmark_by_id(booking["pickup_id"]) or {"name": booking["pickup_id"]}
                        await send_text(sender, "উঠবেন এবং নামবেন একই জায়গা হতে পারে না। অন্য নামার জায়গা বেছে নিন:")
                        await send_drop_menu(sender, pickup_lm)
                        continue
                    booking["drop_id"] = str(drop_lm.get("id"))
                    await send_booking_summary(sender, booking)
                    continue

                if interactive_id in ("confirm_share", "confirm_full"):
                    booking = RIDER_BOOKINGS.get(sender)
                    if not booking or not booking.get("pickup_id") or not booking.get("drop_id"):
                        await send_pickup_menu(sender)
                        continue

                    reserve_full = interactive_id == "confirm_full"
                    fare = calculate_fare(
                        booking["pickup_id"],
                        booking["drop_id"],
                        reserve_full=reserve_full,
                    )
                    pickup_lm = find_landmark_by_id(booking["pickup_id"])
                    stand_id = nearest_stand_for_landmark(pickup_lm)

                    RIDE_COUNTER += 1
                    ride_id = f"RIDE_{RIDE_COUNTER}"
                    RIDES[ride_id] = {
                        "id": ride_id,
                        "rider_phone": sender,
                        "pickup_id": booking["pickup_id"],
                        "drop_id": booking["drop_id"],
                        "reserve_full": reserve_full,
                        "fare": fare,
                        "stand_id": stand_id,
                        "driver_phone": None,
                        "pin": None,
                        "status": "SEARCHING",
                        "created_at": datetime.utcnow().isoformat(),
                    }
                    booking["ride_id"] = ride_id
                    booking["fare"] = fare

                    await send_buttons(
                        sender,
                        "🔍 কাছের স্ট্যান্ডে টোটো খোঁজা হচ্ছে... ২০-৩০ সেকেন্ড অপেক্ষা করুন।",
                        [{"id": "cancel_booking", "title": "Cancel"}],
                    )
                    asyncio.create_task(dispatch_ride(ride_id))
                    continue

                if text:
                    matched_route = await handle_route_text(sender, text)
                    if matched_route:
                        continue
                    single_lm = match_landmark_text(text)
                    if single_lm:
                        RIDER_BOOKINGS[sender] = {"pickup_id": str(single_lm.get("id"))}
                        await send_drop_menu(sender, single_lm)
                        continue

                await send_pickup_menu(sender)

    return {"status": "ok"}
