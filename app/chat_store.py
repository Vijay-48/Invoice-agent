"""Saved assistant conversations (chat_sessions / chat_messages)."""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatMessage, ChatSession

TITLE_CHARS = 60


def serialize_message(m: ChatMessage) -> dict:
    meta = m.meta or {}
    return {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at,
            "tools_used": meta.get("tools_used", []), "files": meta.get("files", []),
            "actions": meta.get("actions", []), "model": meta.get("model")}


def serialize_session(s: ChatSession, messages: int | None = None) -> dict:
    out = {"id": s.id, "title": s.title, "created_at": s.created_at, "updated_at": s.updated_at}
    if messages is not None:
        out["messages"] = messages
    return out


def list_sessions(session: Session, user_id: int, limit: int = 50) -> list[dict]:
    counts = (select(ChatMessage.session_id, func.count().label("n")).group_by(ChatMessage.session_id).subquery())
    rows = session.execute(select(ChatSession, func.coalesce(counts.c.n, 0))
                           .outerjoin(counts, counts.c.session_id == ChatSession.id)
                           .where(ChatSession.user_id == user_id)
                           .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(limit)).all()
    return [serialize_session(s, n) for s, n in rows]


def get_owned(session: Session, user_id: int, session_id: int) -> ChatSession | None:
    s = session.get(ChatSession, session_id)
    return s if s is not None and s.user_id == user_id else None


def messages(session: Session, session_id: int) -> list[ChatMessage]:
    return list(session.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)
                                .order_by(ChatMessage.id)))


def history(session: Session, session_id: int) -> list[dict]:
    """The conversation for the model: user and assistant turns only (errors left out)."""
    return [{"role": m.role, "content": m.content} for m in messages(session, session_id)
            if m.role in ("user", "assistant")]


def add_message(session: Session, chat: ChatSession, role: str, content: str, meta: dict | None = None) -> ChatMessage:
    m = ChatMessage(session_id=chat.id, role=role, content=content, meta=meta)
    session.add(m)
    if role == "user" and chat.title in ("", "New chat"):
        title = " ".join(content.split())
        chat.title = title[:TITLE_CHARS] + ("…" if len(title) > TITLE_CHARS else "")
    chat.updated_at = datetime.now(timezone.utc)
    session.commit()
    return m


def mark_action_done(session: Session, message: ChatMessage, index: int, label: str) -> ChatMessage:
    meta = dict(message.meta or {})
    actions = [dict(a) for a in meta.get("actions", [])]
    if not 0 <= index < len(actions):
        raise IndexError("No such action")
    actions[index]["done"] = label[:120]
    meta["actions"] = actions
    message.meta = meta   # a new dict, so the JSON column is saved
    session.commit()
    return message
