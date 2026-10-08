"""Snap & Study AI - a Streamlit vision chatbot for students.

Flow: onboarding -> Gemini chat (image/text) -> conversation memory ->
Gemini-written summary -> Gmail SMTP email.
"""

import base64
import binascii
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import httpx
import streamlit as st
import extra_streamlit_components as stx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image

import database as store

from prompts import (
    DEFAULT_IMAGE_INSTRUCTION,
    SUMMARY_REQUEST_PROMPT,
    SYSTEM_PROMPT,
    WELCOME_MESSAGE_TEMPLATE,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
st.set_page_config(page_title="Snap & Study AI", page_icon="📚", layout="centered")

ALLOWED_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_UPLOAD_MB = 10
EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
logger = logging.getLogger(__name__)
DB_RETRY_SECONDS = 30
DB_NOT_CONFIGURED_MSG = (
    "Conversation history is unavailable because MongoDB is not configured."
)
DB_UNAVAILABLE_MSG = (
    "Conversation history is temporarily unavailable."
)
DB_FAILED = object()  # sentinel: a database call failed (None means "not found")
MAX_SIDEBAR_TITLE = 30
IDENTITY_COOKIE_NAME = "snap_study_identity"
IDENTITY_COOKIE_DAYS = 30


class UserFacingError(Exception):
    """An error whose message is safe and helpful to show to the student."""


# --------------------------------------------------------------------------
# Secrets / configuration helpers
# --------------------------------------------------------------------------
def get_secret(key: str, default: str = "") -> str:
    """Read a value from st.secrets, falling back to environment variables."""
    try:
        value = st.secrets.get(key)
    except Exception:  # no secrets file / malformed secrets
        value = None
    if value is None:
        value = os.environ.get(key)
    return str(value).strip() if value else default


def get_model_name() -> str:
    return get_secret("GEMINI_MODEL", "gemini-2.5-flash")


def secure_cookie_connection() -> bool:
    """Use Secure cookies when the app is served over HTTPS."""
    return st.context.headers.get("X-Forwarded-Proto", "").lower() == "https"


def identity_signing_key() -> bytes:
    """Use a dedicated cookie secret, or derive a stable key from an app secret."""
    secret = (
        get_secret("IDENTITY_COOKIE_SECRET")
        or get_secret("GEMINI_API_KEY")
        or get_secret("MONGODB_URI")
    )
    if not secret:
        raise UserFacingError("Identity persistence is not configured.")
    return hashlib.sha256(f"snap-study-identity:{secret}".encode("utf-8")).digest()


def encode_identity_cookie(name: str, email: str) -> str:
    payload = {
        "name": name,
        "email": email,
        "expires_at": int(time.time()) + IDENTITY_COOKIE_DAYS * 24 * 60 * 60,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(
        identity_signing_key(), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def decode_identity_cookie(value) -> dict | None:
    """Validate a signed, expiring identity cookie without trusting its contents."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        encoded, signature = value.split(".", 1)
        expected = hmac.new(
            identity_signing_key(), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return None

    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    email = payload.get("email")
    expires_at = payload.get("expires_at")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(email, str)
        or not is_valid_email(email.strip())
        or not isinstance(expires_at, int)
        or expires_at <= time.time()
    ):
        return None
    return {"name": name.strip(), "email": email.strip().lower()}


def restore_identity(cookie_manager) -> None:
    """Restore a student's identity once from the browser's signed cookie."""
    if st.session_state.identity_restored or st.session_state.onboarded:
        return

    cookie_value = cookie_manager.get(IDENTITY_COOKIE_NAME)
    logger.info("Identity cookie present: %s", bool(cookie_value))
    identity = decode_identity_cookie(cookie_value)
    if identity is None:
        if cookie_value:
            logger.warning("Ignoring an invalid or expired identity cookie")
        return

    st.session_state.name = identity["name"]
    st.session_state.email = identity["email"]
    st.session_state.onboarded = True
    st.session_state.identity_restored = True
    st.session_state.active_conversation_id = store.new_id()
    st.session_state.messages = [welcome_message()]
    st.session_state.identity_restore_error = None
    logger.info("Browser identity restored")
    try:
        start_chat_session()
    except UserFacingError as exc:
        logger.error("Could not restore Gemini session: %s", redact_secrets(exc))
        st.session_state.identity_restore_error = str(exc)


def change_student(cookie_manager) -> None:
    """Clear browser identity and session state without deleting saved history."""
    cookie_manager.delete(IDENTITY_COOKIE_NAME, key="delete_identity")
    st.session_state.onboarded = False
    st.session_state.name = ""
    st.session_state.email = ""
    st.session_state.active_conversation_id = store.new_id()
    st.session_state.messages = []
    st.session_state.history = []
    st.session_state.chat = None
    st.session_state.client = None
    st.session_state.has_interacted = False
    st.session_state.last_summary = None
    st.session_state.pending_delete = None
    st.session_state.db_down_until = 0.0
    st.session_state.identity_restored = True
    st.session_state.identity_restore_error = None


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------
def init_state() -> None:
    defaults = {
        "onboarded": False,
        "name": "",
        "email": "",
        "client": None,
        "chat": None,
        "messages": [],
        "has_interacted": False,
        "last_summary": None,
        "active_conversation_id": store.new_id(),
        "history": [],
        "db_down_until": 0.0,
        "pending_delete": None,
        "identity_restored": False,
        "identity_restore_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def add_message(role: str, kind: str, content, mime_type: str | None = None) -> None:
    """Append a message dict to the stored chat history."""
    message = {"role": role, "kind": kind, "content": content}
    if mime_type:
        message["mime_type"] = mime_type
    st.session_state.messages.append(message)


def render_message(message: dict) -> None:
    """Render one stored message (text or image) in the chat UI."""
    with st.chat_message(message["role"]):
        if message["kind"] == "image":
            st.image(message["content"], width=320)
        else:
            st.markdown(message["content"])


def reset_conversation() -> None:
    """Clear the conversation but keep the student's name and email."""
    st.session_state.messages = []
    st.session_state.has_interacted = False
    st.session_state.last_summary = None
    st.session_state.active_conversation_id = store.new_id()
    start_chat_session()


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
def redact_secrets(text: str) -> str:
    """Remove any credential-looking text so errors are safe to display/log."""
    text = str(text)
    for key in ("GEMINI_API_KEY", "GMAIL_APP_PASSWORD", "MONGODB_URI"):
        secret = get_secret(key)
        for variant in {secret, secret.replace(" ", "")}:
            if variant and len(variant) >= 4:
                text = text.replace(variant, "***")
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "***", text)  # Google API key shape
    text = re.sub(r"mongodb(\+srv)?://[^@\s/]+@", "mongodb://***@", text)  # URI credentials
    text = re.sub(r"(?i)(key|token|password)=[^&\s]+", r"\1=***", text)
    return " ".join(text.split())[:300]


def friendly_gemini_error(exc: Exception) -> str:
    """Describe a Gemini failure accurately, with a redacted API detail for debugging."""
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status", None) or ""
        detail = redact_secrets(getattr(exc, "message", None) or exc)
        logger.error("Gemini API error %s %s: %s", code, status, detail)
        if code == 400:
            summary = "Gemini rejected the request as invalid (400)."
        elif code == 401:
            summary = "Gemini authentication failed (401). Check GEMINI_API_KEY."
        elif code == 403:
            summary = (
                "Gemini denied permission (403). Check that GEMINI_API_KEY is valid, "
                "the API is enabled for it, and it is allowed in your region."
            )
        elif code == 404:
            summary = (
                f"Gemini model '{get_model_name()}' was not found (404). "
                "Set a valid GEMINI_MODEL in your secrets."
            )
        elif code == 429:
            summary = "Gemini rate limit or quota reached (429). Wait a moment and retry."
        elif isinstance(code, int) and code >= 500:
            summary = f"Gemini server error ({code}). This is usually temporary; please retry."
        else:
            summary = f"Gemini API error ({code})."
        label = f"{code} {status}".strip()
        return f"{summary} [API response: {label}: {detail}]" if detail else summary
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "The request to Gemini timed out. Please try again."
    if isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
        return "Could not reach Gemini (network problem). Check your connection and try again."
    logger.error("Unexpected Gemini failure: %s: %s", type(exc).__name__, redact_secrets(exc))
    return f"Unexpected error talking to Gemini ({type(exc).__name__}): {redact_secrets(exc)}"


def start_chat_session(history: list | None = None) -> None:
    """Create the Gemini client (once) and a chat with the system prompt.

    ``history`` seeds the chat when restoring a saved conversation.
    """
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise UserFacingError(
            "GEMINI_API_KEY is missing. Add it to .streamlit/secrets.toml "
            "(local) or to your app's Secrets (Streamlit Cloud)."
        )
    try:
        if st.session_state.client is None:
            st.session_state.client = genai.Client(api_key=api_key)
        st.session_state.chat = st.session_state.client.chats.create(
            model=get_model_name(),
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            history=history,
        )
    except Exception as exc:
        raise UserFacingError(friendly_gemini_error(exc)) from exc


def ask_gemini(parts: list) -> str:
    """Send a message to the persistent chat session and return the reply text."""
    try:
        response = st.session_state.chat.send_message(parts)
    except Exception as exc:
        raise UserFacingError(friendly_gemini_error(exc)) from exc
    text = (response.text or "").strip() if response is not None else ""
    if not text:
        raise UserFacingError(
            "Gemini returned an empty response (it may have been blocked). "
            "Try rephrasing your question or using a clearer image."
        )
    return text


def generate_summary() -> str:
    """Summarize the conversation in a SEPARATE chat session.

    The live chat (st.session_state.chat) is never modified: a deep copy of its
    history seeds a throwaway chat, and the summary prompt is sent only there.
    """
    live_history = [c.model_copy(deep=True) for c in st.session_state.chat.get_history()]
    if not live_history:
        raise UserFacingError("There is no conversation to summarize yet.")
    try:
        summary_chat = st.session_state.client.chats.create(
            model=get_model_name(),
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
            history=live_history,
        )
        response = summary_chat.send_message(
            SUMMARY_REQUEST_PROMPT.format(name=st.session_state.name)
        )
    except Exception as exc:
        raise UserFacingError(friendly_gemini_error(exc)) from exc
    text = (response.text or "").strip() if response is not None else ""
    if not text:
        raise UserFacingError("Gemini could not produce a summary. Please try again.")
    return text


# --------------------------------------------------------------------------
# Image handling
# --------------------------------------------------------------------------
def read_image(uploaded_file) -> tuple[bytes, str]:
    """Validate an uploaded file and return (bytes, mime_type)."""
    data = uploaded_file.getvalue()
    if not data:
        raise UserFacingError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise UserFacingError(f"Image is too large. Please upload under {MAX_UPLOAD_MB} MB.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img_format = (img.format or "").upper()
            img.verify()
    except Exception as exc:
        raise UserFacingError(
            "That file could not be read as an image. Please upload a valid JPG, PNG or WEBP."
        ) from exc
    if img_format not in ALLOWED_FORMATS:
        raise UserFacingError("Unsupported image type. Please upload a JPG, PNG or WEBP.")
    return data, ALLOWED_FORMATS[img_format]


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def is_valid_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email)) and len(email) <= 254


def send_summary_email(to_email: str, student_name: str, summary: str) -> None:
    """Send the summary through Gmail SMTP (SSL, port 465)."""
    sender = get_secret("GMAIL_ADDRESS")
    app_password = get_secret("GMAIL_APP_PASSWORD").replace(" ", "")
    if not sender or not app_password:
        raise UserFacingError(
            "Email is not configured. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "to your secrets (see the README)."
        )
    if not is_valid_email(to_email):
        raise UserFacingError("The recipient email address looks invalid.")

    body = f"Hi {student_name},\n\nHere are your Snap & Study notes:\n\n{summary}\n\nHappy studying!\n- Snap & Study AI"
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5;'
        f'white-space:pre-wrap;">{html.escape(body)}</div>'
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "📚 Your Snap & Study notes"
    msg["From"] = formataddr(("Snap & Study AI", sender))
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
            server.login(sender, app_password)
            server.sendmail(sender, [to_email], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise UserFacingError(
            "Gmail rejected the login. Make sure you are using a 16-character "
            "App Password (not your normal password) and that 2-Step Verification is on."
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise UserFacingError("Gmail refused the recipient address. Please check your email.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise UserFacingError("Could not send the email right now. Please try again.") from exc


# --------------------------------------------------------------------------
# Persistent history (MongoDB)
# --------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_mongo_db(uri: str, database_name: str):
    """One MongoDB connection per server process (cached across reruns/sessions)."""
    client = store.create_client(uri)
    try:
        db = client[database_name]
        client.admin.command("ping")
        logger.info("MongoDB connection: successful")
        logger.info("Database: %s", database_name)
        store.ensure_indexes(db)
        logger.info("MongoDB indexes ensured")
        return db
    except Exception:
        client.close()
        raise  # exceptions are not cached, so a later rerun can retry


def db_enabled() -> bool:
    configured = bool(get_secret("MONGODB_URI"))
    logger.debug("MongoDB configured: %s", configured)
    return configured


def db_is_down() -> bool:
    return time.time() < st.session_state.db_down_until


def db_call(fn, *args, default=DB_FAILED, **kwargs):
    """Run a store function; never raises. Returns `default` on any DB problem."""
    if not db_enabled() or db_is_down():
        return default
    try:
        db = get_mongo_db(
            get_secret("MONGODB_URI"), get_secret("MONGODB_DATABASE", "snap_and_study")
        )
        return fn(db, *args, **kwargs)
    except Exception as exc:
        logger.error("MongoDB error in %s: %s", getattr(fn, "__name__", "op"), redact_secrets(exc))
        st.session_state.db_down_until = time.time() + DB_RETRY_SECONDS
        return default


def persist_new_messages(new_messages: list[dict], first_user_text: str | None) -> None:
    """Auto-save freshly added messages to the active conversation."""
    logger.info("Conversation save attempted")
    result = db_call(
        store.save_messages,
        st.session_state.email,
        st.session_state.name,
        st.session_state.active_conversation_id,
        new_messages,
        first_user_text,
        default=DB_FAILED,
    )
    if result is DB_FAILED:
        logger.warning("Conversation save failed")
        return
    logger.info("Conversation save successful")


def refresh_history() -> None:
    """Always rebuild the sidebar list from MongoDB, never from stale session data."""
    if not db_enabled():
        st.session_state.history = []
        return

    result = db_call(store.list_conversations, st.session_state.email)
    if result is not DB_FAILED:
        st.session_state.history = result
        logger.info("History query returned: %d conversations", len(result))
    else:
        st.session_state.history = []


def build_gemini_history(messages: list[dict]) -> list:
    """Rebuild Gemini chat history (user/model turns) from stored messages."""
    history, pending, has_image, has_text = [], [], False, False
    for m in messages:
        if m["role"] == "user":
            if m["kind"] == "image":
                pending.append(types.Part.from_bytes(data=m["content"], mime_type=m["mime_type"]))
                has_image = True
            else:
                pending.append(types.Part.from_text(text=m["content"]))
                has_text = True
        else:
            if pending:
                if has_image and not has_text:  # image-only turn used the default prompt
                    pending.append(types.Part.from_text(text=DEFAULT_IMAGE_INSTRUCTION))
                history.append(types.Content(role="user", parts=pending))
                pending, has_image, has_text = [], False, False
            history.append(types.Content(role="model", parts=[types.Part.from_text(text=m["content"])]))
    return history


def welcome_message() -> dict:
    return {
        "role": "assistant",
        "kind": "text",
        "content": WELCOME_MESSAGE_TEMPLATE.format(name=st.session_state.name),
    }


def new_conversation() -> None:
    """Start a fresh conversation. Saved conversations are left untouched."""
    reset_conversation()
    st.session_state.messages.append(welcome_message())


def open_conversation(conversation_id: str) -> None:
    """Load a saved conversation and restore chat UI + Gemini context."""
    data = db_call(store.load_conversation, st.session_state.email, conversation_id)
    if data is DB_FAILED:
        return  # friendly notice is shown by chat_screen via db_is_down()
    if data is None:
        raise UserFacingError("That conversation could not be found.")
    old_chat = st.session_state.chat
    try:
        start_chat_session(history=build_gemini_history(data["messages"]))
    except UserFacingError:
        st.session_state.chat = old_chat
        raise
    st.session_state.active_conversation_id = data["conversation_id"]
    st.session_state.messages = [welcome_message()] + data["messages"]
    st.session_state.has_interacted = True
    st.session_state.last_summary = None


def delete_conversation(conversation_id: str) -> None:
    result = db_call(store.delete_conversation, st.session_state.email, conversation_id)
    if result is DB_FAILED:
        return
    st.session_state.history = [
        c for c in st.session_state.history if c["conversation_id"] != conversation_id
    ]
    if conversation_id == st.session_state.active_conversation_id:
        new_conversation()


def local_timezone():
    """The browser's timezone when available (for Today/Yesterday), else UTC."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(st.context.timezone)
    except Exception:
        return timezone.utc


def date_group(updated_at, tz) -> str:
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    days = (datetime.now(tz).date() - updated_at.astimezone(tz).date()).days
    return "Today" if days <= 0 else "Yesterday" if days == 1 else "Earlier"


def short_title(title: str) -> str:
    return title if len(title) <= MAX_SIDEBAR_TITLE else title[: MAX_SIDEBAR_TITLE - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def render_sidebar(cookie_manager) -> None:
    with st.sidebar:
        st.markdown("### 📚 Snap & Study")
        if not db_enabled():
            st.warning(DB_NOT_CONFIGURED_MSG)
        elif db_is_down():
            st.warning(DB_UNAVAILABLE_MSG)
        if not st.session_state.onboarded:
            return

        if st.button("+ New conversation", use_container_width=True, type="secondary"):
            try:
                new_conversation()
            except UserFacingError as exc:
                st.error(str(exc))
            else:
                st.rerun()

        st.caption("HISTORY")
        refresh_history()
        history = st.session_state.history

        pending = st.session_state.pending_delete
        if pending:
            target = next((c for c in history if c["conversation_id"] == pending), None)
            if target is None:
                st.session_state.pending_delete = None
            else:
                st.warning(f"Delete “{short_title(target['title'])}” and its images?")
                yes, no = st.columns(2)
                if yes.button("Delete", key="confirm_delete", use_container_width=True):
                    st.session_state.pending_delete = None
                    delete_conversation(pending)
                    st.rerun()
                if no.button("Cancel", key="cancel_delete", use_container_width=True):
                    st.session_state.pending_delete = None
                    st.rerun()

        if db_enabled() and not db_is_down() and not history:
            st.caption("Your conversations will appear here.")

        tz = local_timezone()
        current_group = None
        for conv in history:
            group = date_group(conv["updated_at"], tz)
            if group != current_group:
                st.caption(group)
                current_group = group
            cid = conv["conversation_id"]
            open_col, del_col = st.columns([5, 1], vertical_alignment="center")
            is_active = cid == st.session_state.active_conversation_id
            if open_col.button(
                f"📝 {short_title(conv['title'])}",
                key=f"open_{cid}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
                help=conv["title"],
            ):
                try:
                    open_conversation(cid)
                except UserFacingError as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
            if del_col.button("🗑", key=f"del_{cid}", help="Delete conversation"):
                st.session_state.pending_delete = cid
                st.rerun()

        st.divider()
        st.markdown(f"👤 **{st.session_state.name}**")
        st.caption(st.session_state.email)
        if st.button("Change student", use_container_width=True):
            change_student(cookie_manager)


def onboarding_screen(cookie_manager) -> None:
    st.title("📚 Snap & Study AI")
    st.subheader("Snap it. Understand it. Master it.")
    st.write("Tell us who you are so we can send your study notes to your inbox.")

    with st.form("onboarding_form"):
        name = st.text_input("Student Name")
        email = st.text_input("Email Address")
        submitted = st.form_submit_button("Start Studying 🚀", use_container_width=True)

    if not submitted:
        return

    name, email = name.strip(), email.strip().lower()
    if not name:
        st.error("Please enter your name.")
        return
    if not email or not is_valid_email(email):
        st.error("Please enter a valid email address (for example, you@example.com).")
        return

    st.session_state.name = name
    st.session_state.email = email
    try:
        with st.spinner("Setting up your study assistant..."):
            start_chat_session()
        cookie_manager.set(
            IDENTITY_COOKIE_NAME,
            encode_identity_cookie(name, email),
            key="set_identity_cookie",
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=IDENTITY_COOKIE_DAYS),
            secure=secure_cookie_connection(),
            same_site="strict",
        )
    except UserFacingError as exc:
        st.error(str(exc))
        return

    add_message("assistant", "text", WELCOME_MESSAGE_TEMPLATE.format(name=name))
    st.session_state.active_conversation_id = store.new_id()
    st.session_state.onboarded = True
    st.session_state.identity_restored = True
    st.session_state.identity_restore_error = None


def handle_email_click() -> None:
    try:
        with st.spinner("Writing your study notes and sending the email..."):
            summary = generate_summary()
            send_summary_email(st.session_state.email, st.session_state.name, summary)
    except UserFacingError as exc:
        st.error(str(exc))
        return
    st.session_state.last_summary = summary
    st.success(f"✅ Study notes sent to {st.session_state.email}. Check your inbox (and spam folder).")


def get_latest_image_from_history() -> tuple[bytes | None, str | None]:
    """Return (bytes, mime_type) of the most recent image in the conversation."""
    for message in reversed(st.session_state.messages):
        if message["kind"] == "image":
            return message["content"], message.get("mime_type") or "image/png"
    return None, None


def handle_user_input(prompt) -> None:
    """`prompt` is the ChatInputValue from st.chat_input (use .text / .files)."""
    text = (prompt.text or "").strip()
    files = prompt.files or []

    if not text and not files:
        st.warning("Please type a question or upload an image.")
        return

    parts, image_bytes, mime_type = [], None, None  # image_bytes = NEW upload only
    try:
        if files:
            image_bytes, mime_type = read_image(files[0])
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        else:
            # Text-only follow-up: re-attach the latest image (if any) for Gemini
            # only. It is NOT stored or displayed again.
            prev_bytes, prev_mime = get_latest_image_from_history()
            if prev_bytes:
                parts.append(types.Part.from_bytes(data=prev_bytes, mime_type=prev_mime))
        parts.append(
            types.Part.from_text(text=text if text else DEFAULT_IMAGE_INSTRUCTION)
        )
    except UserFacingError as exc:
        st.error(str(exc))
        return

    # Show the student's input immediately (stored only after Gemini succeeds).
    if image_bytes:
        render_message({"role": "user", "kind": "image", "content": image_bytes})
    if text:
        render_message({"role": "user", "kind": "text", "content": text})

    try:
        with st.spinner("Gemini is studying your material..."):
            reply = ask_gemini(parts)
    except UserFacingError as exc:
        st.error(f"{exc} Your message was not sent; please try again.")
        return

    n_before = len(st.session_state.messages)
    if image_bytes:
        add_message("user", "image", image_bytes, mime_type)
    if text:
        add_message("user", "text", text)
    add_message("assistant", "text", reply)
    st.session_state.has_interacted = True
    persist_new_messages(st.session_state.messages[n_before:], text or None)
    st.rerun()


def chat_screen() -> None:
    title_col, button_col = st.columns([3, 2], vertical_alignment="center")
    with title_col:
        st.title("📚 Snap & Study AI")
    with button_col:
        email_clicked = st.button(
            "📧 Send Explanation to Email",
            type="primary",
            disabled=not st.session_state.has_interacted,
            use_container_width=True,
            help="Available after you've asked the AI something.",
        )
    st.caption(
        "Upload a problem, diagram, or notes and let AI teach you. "
        "AI can make mistakes, so double-check important answers."
    )
    st.caption(
        f"Logged in as {st.session_state.name} • "
        f"Explanations will be sent to {st.session_state.email}"
    )

    if db_enabled() and db_is_down():
        st.warning(DB_UNAVAILABLE_MSG)

    if email_clicked:
        handle_email_click()
    if st.session_state.last_summary:
        with st.expander("View the last summary sent to your email"):
            st.text(st.session_state.last_summary)

    for message in st.session_state.messages:
        render_message(message)

    prompt = st.chat_input(
        "Ask a question or attach a photo of your study material...",
        accept_file=True,
        file_type=ALLOWED_EXTENSIONS,
        max_upload_size=MAX_UPLOAD_MB,
    )
    if prompt:
        handle_user_input(prompt)


def main() -> None:
    init_state()
    cookie_manager = stx.CookieManager(key="identity_cookie_manager")
    restore_identity(cookie_manager)
    render_sidebar(cookie_manager)
    if not st.session_state.onboarded:
        onboarding_screen(cookie_manager)
    else:
        if st.session_state.identity_restore_error:
            st.error(st.session_state.identity_restore_error)
        chat_screen()


main()
