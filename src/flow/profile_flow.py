"""ProfileFlow — the stateful /creare_profile onboarding conversation.

The bot is stateless (GitHub Actions poll-based), so the "which step is this
user on" state is persisted in the Store's meta kv, keyed by chat id. A
/creare_profile message starts the flow and sends the instructions; each
subsequent message from that chat is routed to the current step (papers ->
authors -> topics) until the flow completes. /annulla aborts.

Step logic lives in the per-step handlers (src/flow/step_*.py); this class only
owns the state machine, the user-facing prompts, and routing.
"""

from __future__ import annotations

from src.enrich.arxiv_resolver import resolve_title_to_id
from src.flow.step_authors import handle_authors
from src.flow.step_papers import handle_papers
from src.flow.step_topics import handle_topics

INSTRUCTIONS = (
    "📝 Let's set up your profile in 3 steps — reply to each step with a message.\n\n"
    "1️⃣  Send me the papers you've already read, *one title per line*.\n"
    "    I'll look up their arXiv ids and save them to your profile (I'll flag any that aren't on arXiv).\n\n"
    "Then I'll ask for:\n"
    "2️⃣  your favorite authors\n"
    "3️⃣  the topics you care about most\n\n"
    "✍️  Start now: send me the titles of the papers you've read (one per line).\n"
    "You can cancel anytime with /annulla."
)
_PROMPT_AUTHORS = "2️⃣  Now send me your favorite authors — one per line or comma-separated."
_PROMPT_TOPICS = "3️⃣  Finally, send me the topics you care about most — one per line or comma-separated."
_DONE = (
    "✅ Profile created! I'll use these papers, authors, and topics for recommendations.\n"
    "You can edit it anytime with /add_* and /remove_*."
)


class ProfileFlow:
    def __init__(self, store, profile_store, resolver=resolve_title_to_id) -> None:
        self.store = store
        self.profile_store = profile_store
        self.resolver = resolver

    def _key(self, chat_id, scope_id=None) -> str:
        return f"flow:{scope_id or chat_id}"

    def active_step(self, chat_id, scope_id=None) -> str | None:
        return self.store.get_meta(self._key(chat_id, scope_id)) or None

    def start(self, chat_id, scope_id=None) -> str:
        self.store.set_meta(self._key(chat_id, scope_id), "papers")
        return INSTRUCTIONS

    def maybe_handle(self, chat_id, text: str, profile_store=None, scope_id=None) -> str | None:
        """Return a reply if this message belongs to the flow, else None.

        None lets the caller fall back to the normal command dispatcher.
        """
        profile_store = profile_store or self.profile_store
        stripped = (text or "").strip()
        command = stripped.split()[0].split("@", 1)[0].lower() if stripped.startswith("/") else ""

        if command == "/creare_profile":
            return self.start(chat_id, scope_id)
        if command in ("/annulla", "/cancel"):
            if self.active_step(chat_id, scope_id):
                self.store.set_meta(self._key(chat_id, scope_id), "")
                return "Profile setup canceled."
            return None

        step = self.active_step(chat_id, scope_id)
        # A slash-command mid-onboarding (/start, /help, ...) is NOT step input:
        # hand it to the dispatcher and keep the flow on the same step, so a stray
        # command can't pollute the profile (e.g. "/start" saved as an author/keyword).
        if step and command:
            return None
        if step == "papers":
            reply = handle_papers(stripped, profile_store, self.resolver)
            self.store.set_meta(self._key(chat_id, scope_id), "authors")
            return f"{reply}\n\n{_PROMPT_AUTHORS}"
        if step == "authors":
            reply = handle_authors(stripped, profile_store)
            self.store.set_meta(self._key(chat_id, scope_id), "topics")
            return f"{reply}\n\n{_PROMPT_TOPICS}"
        if step == "topics":
            reply = handle_topics(stripped, profile_store)
            self.store.set_meta(self._key(chat_id, scope_id), "")  # flow complete
            return f"{reply}\n\n{_DONE}"
        return None
