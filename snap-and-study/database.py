"""MongoDB persistence for Snap & Study AI.

Collections
-----------
conversations  one document per conversation (text messages + image references)
images         one document per uploaded image (kept out of the conversation
               document so it can never approach MongoDB's 16 MB limit)

Every read/write is scoped by the student's (normalized) email, so one student
can never load or delete another student's conversation, even by guessing an id.
All functions take a pymongo ``Database`` and contain no Streamlit code.
"""

import re
import uuid
from datetime import datetime, timezone

from bson import Binary
from pymongo import MongoClient

CONVERSATIONS = "conversations"
IMAGES = "images"
TEMP_TITLE = "Image Study Session"
FALLBACK_TITLE = "Study Session"
MAX_TITLE_LEN = 40


def create_client(uri: str) -> MongoClient:
    """Build a MongoClient (lazy connect; intended to be cached by the caller)."""
    return MongoClient(
        uri, serverSelectionTimeoutMS=5000, tz_aware=True, appname="snap-and-study"
    )


def ensure_indexes(db) -> None:
    db[CONVERSATIONS].create_index("conversation_id", unique=True)
    db[CONVERSATIONS].create_index([("user_email", 1), ("updated_at", -1)])
    db[IMAGES].create_index("image_id", unique=True)
    db[IMAGES].create_index([("conversation_id", 1), ("user_email", 1)])


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Titles (local, no extra Gemini call)
# --------------------------------------------------------------------------
_FILLER = re.compile(
    r"^(?:(?:step by step|hi|hello|hey|please|pls|kindly|can|could|would|will|you|me|us|i|we|"
    r"want|need|to|help|tell|explain|give|show|write|solve|teach|what|whats|what's|"
    r"is|are|the|a|an|how|why|do|does|about|of|this|that|these|those|following|"
    r"problem|question|and|then|please)\b[\s,:\-]*)+",
    re.IGNORECASE,
)


_TRAILING = {"and", "in", "of", "for", "with", "to", "the", "a", "an", "on", "at", "or", "by"}


def generate_title(text: str | None) -> str:
    """Derive a short title from the first meaningful user question."""
    if not text or not text.strip():
        return TEMP_TITLE
    cleaned = re.sub(r"[^\w\s+#./&()\-]", " ", text.replace("_", " "))
    cleaned = " ".join(cleaned.split())
    core = _FILLER.sub("", cleaned).strip() or cleaned
    words = core.split()[:6]
    while len(words) > 1 and words[-1].lower() in _TRAILING:
        words.pop()
    if not words:
        return FALLBACK_TITLE
    title = " ".join(w[0].upper() + w[1:] for w in words)
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rsplit(" ", 1)[0] or title[:MAX_TITLE_LEN]
    return title


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
def save_messages(
    db,
    email: str,
    name: str,
    conversation_id: str,
    messages: list[dict],
    first_user_text: str | None = None,
) -> None:
    """Append messages to a conversation, creating it on first save (upsert).

    ``messages`` items: {"role", "kind": "text"|"image", "content", "mime_type"?}.
    Image bytes go to the images collection; the message keeps only an image_id.
    """
    email = normalize_email(email)
    title = generate_title(first_user_text)
    image_ids: list[str] = []
    docs: list[dict] = []
    try:
        for m in messages:
            doc = {"role": m["role"], "kind": m["kind"], "timestamp": _now()}
            if m["kind"] == "image":
                image_id = new_id()
                mime = m.get("mime_type") or "image/png"
                db[IMAGES].insert_one(
                    {
                        "image_id": image_id,
                        "conversation_id": conversation_id,
                        "user_email": email,
                        "mime_type": mime,
                        "data": Binary(m["content"]),
                        "created_at": _now(),
                    }
                )
                image_ids.append(image_id)
                doc.update(content="", image_id=image_id, mime_type=mime)
            else:
                doc["content"] = m["content"]
            docs.append(doc)

        now = _now()
        key = {"conversation_id": conversation_id, "user_email": email}
        db[CONVERSATIONS].update_one(
            key,
            {
                "$push": {"messages": {"$each": docs}},
                "$set": {"updated_at": now, "user_name": name},
                "$setOnInsert": {"created_at": now, "title": title},
            },
            upsert=True,
        )
        # An image-only start gets a temporary title; upgrade it once a real
        # question arrives.
        if title != TEMP_TITLE:
            db[CONVERSATIONS].update_one(
                {**key, "title": TEMP_TITLE}, {"$set": {"title": title}}
            )
    except Exception:
        if image_ids:  # don't leave orphaned images behind
            try:
                db[IMAGES].delete_many({"image_id": {"$in": image_ids}})
            except Exception:
                pass
        raise


def list_conversations(db, email: str, limit: int = 100) -> list[dict]:
    """Newest-first list of {conversation_id, title, updated_at} for one student."""
    cursor = (
        db[CONVERSATIONS]
        .find(
            {"user_email": normalize_email(email)},
            {"_id": 0, "conversation_id": 1, "title": 1, "updated_at": 1},
        )
        .sort("updated_at", -1)
        .limit(limit)
    )
    return list(cursor)


def load_conversation(db, email: str, conversation_id: str) -> dict | None:
    """Load one conversation with image bytes resolved, or None if not found."""
    email = normalize_email(email)
    doc = db[CONVERSATIONS].find_one(
        {"conversation_id": conversation_id, "user_email": email}, {"_id": 0}
    )
    if not doc:
        return None
    ids = [m["image_id"] for m in doc.get("messages", []) if m.get("image_id")]
    images = {}
    if ids:
        for img in db[IMAGES].find({"image_id": {"$in": ids}, "user_email": email}):
            images[img["image_id"]] = img

    messages = []
    for m in doc.get("messages", []):
        if m.get("kind") == "image":
            img = images.get(m.get("image_id"))
            if img is None:  # image was removed; skip rather than break the chat
                continue
            messages.append(
                {
                    "role": m["role"],
                    "kind": "image",
                    "content": bytes(img["data"]),
                    "mime_type": img.get("mime_type") or "image/png",
                }
            )
        else:
            messages.append(
                {"role": m["role"], "kind": "text", "content": m.get("content", "")}
            )
    return {
        "conversation_id": doc["conversation_id"],
        "title": doc.get("title", FALLBACK_TITLE),
        "messages": messages,
    }


def delete_conversation(db, email: str, conversation_id: str) -> bool:
    """Delete one conversation and its images. Returns True if it existed."""
    email = normalize_email(email)
    result = db[CONVERSATIONS].delete_one(
        {"conversation_id": conversation_id, "user_email": email}
    )
    if result.deleted_count:
        db[IMAGES].delete_many({"conversation_id": conversation_id, "user_email": email})
        return True
    return False
