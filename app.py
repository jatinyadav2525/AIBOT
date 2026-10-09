"""What Did I Miss? Streamlit interface for the local chat analysis engine."""
import hashlib
import html
import math
import re
import time as clock_time
from datetime import datetime, time, timedelta
from pathlib import Path

import streamlit as st

import engine


LIMIT = 15
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
OTHER_ME = "I haven't written in this chat (type my name below)"


def fmt(value):
    return value.strftime("%a %d %b, %I:%M %p")


def estimated_summary_calls(message_count, chars, image_count=0):
    """Approximate map/reduce requests; long chats need extra summary compaction calls."""
    if message_count <= 0:
        return 0
    chunks = max(1, math.ceil(max(chars, 1) / 3000))
    calls = chunks + 1 + image_count  # chunk analysis, final summary, optional image descriptions
    reduced = chunks
    while reduced > 12:
        reduced = math.ceil(reduced / 12)
        calls += reduced
    return calls


def duration_label(seconds):
    low, high = max(1, int(seconds * 0.7)), max(2, int(seconds * 1.5))
    if high < 60:
        return f"about {low}–{high} seconds"
    low_min, high_min = max(1, round(low / 60)), max(1, round(high / 60))
    return f"about {low_min}–{high_min} minutes"


def remaining_time(seconds):
    """Format a single moving ETA for the live processing panel."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"~{max(1, seconds)} sec left"
    minutes = round(seconds / 60)
    return f"~{max(1, minutes)} min left"


def show_work_status(slot, progress, message, done=False, elapsed=0, remaining=0, examples=None):
    progress = min(1.0, max(0.0, float(progress)))
    safe_message = html.escape(str(message))
    percent = round(progress * 100)
    examples = examples or [
        ("CHAT", "Reading messages"),
        ("CHAT", "Finding key moments"),
        ("CHAT", "Building your catch-up"),
    ]
    bubbles = "".join(
        f'<div class="stream-bubble bubble-{index + 1}"><span>{html.escape(str(label))}</span>'
        f'{html.escape(str(text))}</div>'
        for index, (label, text) in enumerate(examples[:3])
    )
    if done:
        title, icon, state_class = "Your catch-up is ready", "✓", "work-done"
    elif progress >= 0.92:
        title, icon, state_class = "Putting the story together", "✦", ""
    elif progress >= 0.4:
        title, icon, state_class = "Connecting the conversation", "✦", ""
    else:
        title, icon, state_class = "Getting things ready", "✦", ""
    eyebrow = "ANALYSIS COMPLETE" if done else "LIVE CHAT ANALYSIS · PROCESSING"
    slot.markdown(
        f'<div class="work-panel {state_class}"><div class="work-scene" aria-hidden="true">'
        f'{bubbles}<div class="stream-scan"></div><div class="stream-core">{icon}</div></div>'
        f'<div class="work-content"><div class="work-eyebrow">{eyebrow}</div>'
        f'<div class="work-title">{title}</div><div class="work-message">{safe_message}</div>'
        f'<div class="work-track"><div class="work-fill" style="width:{percent}%"></div></div>'
        f'<div class="work-foot"><span>{percent}% complete</span>'
        f'<span>{remaining_time(remaining) if not done else "Ready"} · {remaining_time(elapsed).replace(" left", " elapsed") if elapsed else "Starting"}</span></div></div></div>',
        unsafe_allow_html=True,
    )


def summary_html(summary):
    """Render model bullets as safe, styled HTML while preserving emphasis."""
    blocks = []
    for line in str(summary).splitlines():
        line = line.strip()
        if not line:
            continue
        bullet = line.startswith(("- ", "* ", "• "))
        content = html.escape(line[2:] if bullet else line)
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
        blocks.append(
            f'<div class="summary-line"><span class="summary-mark">✦</span><div>{content}</div></div>'
            if bullet else f'<p class="summary-paragraph">{content}</p>'
        )
    return "".join(blocks)


def sender_bars(sender_counts):
    if not sender_counts:
        return '<div class="empty-state">No messages in this period.</div>'
    maximum = max(count for _, count in sender_counts) or 1
    total = sum(count for _, count in sender_counts)
    rows = []
    for index, (name, count) in enumerate(sender_counts):
        pct = count / total * 100 if total else 0
        width = count / maximum * 100
        words = str(name).strip().split()
        initial_words = [words[0], words[-1]] if len(words) > 1 else words[:1]
        initials = "".join(word[0] for word in initial_words if word).upper()[:2] or "?"
        rows.append(
            f'<div class="sender-row"><div class="sender-label"><div class="sender-person">'
            f'<span class="sender-avatar tone-{index % 4}">{safe(initials)}</span><span>{safe(name)}</span></div>'
            f'<b>{count:,} <small>messages · {pct:.0f}%</small></b></div>'
            f'<div class="sender-track"><div class="sender-fill" style="width:{width:.1f}%"></div></div></div>'
        )
    return "".join(rows)


def safe(value):
    return html.escape(str(value)).replace("$", "&#36;").replace("\n", "<br>")


def highlight(text, names):
    names = [name for name in names if name]
    if not names:
        return safe(text)
    pattern = re.compile(
        r"(?<!\w)@?(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)",
        re.I,
    )
    output, pos = [], 0
    for match in pattern.finditer(text):
        output.append(safe(text[pos:match.start()]))
        output.append(f'<span class="mention">{safe(match.group(0))}</span>')
        pos = match.end()
    output.append(safe(text[pos:]))
    return "".join(output)


def delta(value):
    seconds = int(abs(value.total_seconds()))
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def due_text(message, report):
    deadline = message.get("deadline")
    if not deadline:
        return ""
    if message.get("overdue"):
        return f"Was due {fmt(deadline)} · {delta(report['ref'] - deadline)} ago"
    return f"Due {fmt(deadline)} · in {delta(deadline - report['ref'])}"


def render_card(message, report, names):
    deadline = message.get("deadline")
    due = (
        f'<span class="chip {"red" if message.get("overdue") else ""}">{safe(due_text(message, report))}</span>'
        if deadline else ""
    )
    task = f'<div class="meta">AI task: {safe(message.get("llm_task"))}</div>' if message.get("llm_task") else ""
    chips = "".join(f'<span class="chip">{safe(reason)}</span>' for reason in message.get("reasons", []))
    priority = {"high": "High priority", "med": "Worth a look", "low": "Context"}.get(message.get("bucket"), "Context")
    st.markdown(
        f'<div class="card {safe(message.get("bucket", "low"))} {"done" if message.get("handled") else ""}">'
        f'<div class="meta"><b>{safe(message.get("who", "Unknown"))}</b> · {fmt(message["t"])}'
        f'{" · you replied" if message.get("handled") else ""} · {priority}</div>'
        f'<div class="message">{highlight(message.get("text", ""), names)}</div>{task}{due}'
        f'<div>{chips}</div></div>', unsafe_allow_html=True,
    )
    media_name = message.get("media")
    image = report.get("media", {}).get(media_name) if media_name else None
    if image and media_name.lower().endswith(IMAGE_EXTENSIONS):
        st.image(image, width=240)
    index = message.get("i", -1)
    with st.expander("Show conversation around this message"):
        surrounding = report.get("msgs", [])[max(0, index - 2):index + 3]
        for item in surrounding:
            marker = "▶ " if item.get("i") == index else "   "
            st.text(f"{marker}{item.get('who', 'Unknown')}: {item.get('text', '')[:200]}")


def render_rows(items, report):
    for message in items:
        deadline = message.get("deadline")
        when = fmt(deadline or message["t"])
        extra = f' <span class="meta">({safe(due_text(message, report))})</span>' if deadline else ""
        st.markdown(
            f'<div class="row"><b>{when}</b>{extra}<br>{safe(message.get("who", "Unknown"))}: '
            f'{safe(message.get("text", "")[:240])}</div>', unsafe_allow_html=True,
        )


def uploaded_files(uploaded, sample_selected):
    if uploaded:
        return [(file.name, file.getvalue()) for file in uploaded]
    sample = Path(__file__).with_name("sample_chat.txt")
    if sample_selected and sample.is_file():
        return [(sample.name, sample.read_bytes())]
    return []


def load_chat(files):
    """Load files and normalize optional engine results to predictable types."""
    result = engine.load_files(files)
    if not isinstance(result, (tuple, list)) or len(result) != 4:
        raise ValueError("engine.load_files() must return (messages, day_first, media, preview).")
    msgs, dmy, media, preview = result
    msgs = list(msgs or [])
    media = media if isinstance(media, dict) else {}
    preview = preview if isinstance(preview, str) else str(preview or "")
    participants = engine.participants(msgs) or []
    people = []
    for item in participants:
        if isinstance(item, (tuple, list)) and item:
            name = item[0]
        else:
            name = item
        if name is not None and str(name).strip():
            people.append(str(name).strip())
    # Recover participant names from messages if an engine version returns None or an empty list.
    if not people:
        people = list(dict.fromkeys(
            str(message.get("who", "")).strip() for message in msgs
            if isinstance(message, dict) and str(message.get("who", "")).strip()
        ))
    return msgs, bool(dmy), media, preview, people


def content_signature(files):
    """Return a stable key that changes when any uploaded content changes."""
    digest = hashlib.sha256()
    for name, data in files:
        encoded_name = name.encode("utf-8", errors="replace")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:16]


def processing_examples(messages):
    """Pick three spread-out, escaped snippets for the decorative processing stream."""
    if not messages:
        return [("CHAT", "Preparing your conversation")]
    indexes = sorted({0, len(messages) // 2, len(messages) - 1})
    examples = []
    for index in indexes:
        message = messages[index]
        speaker = str(message.get("who", "CHAT")).strip() or "CHAT"
        text = str(message.get("text", "")).replace("\n", " ").strip()
        if not text or text.startswith("<media omitted>"):
            text = "Shared an image or attachment"
        examples.append((speaker[:20], text[:76] + ("…" if len(text) > 76 else "")))
    return examples


def render_background(slot, mode_class, examples=None, processing=False):
    """Draw the full-screen animated conversation layer behind the Streamlit UI."""
    if not examples:
        examples = [
            ("ALEX", "Are we still meeting after class?"),
            ("YOU", "Yes, 5:30 works for me."),
            ("CHAT", "Plans · tone · next steps"),
            ("ALEX", "How did your presentation go?"),
            ("YOU", "Better than I expected ✨"),
        ]
    bubbles = "".join(
        f'<div class="bg-message m{index + 1}"><b>{html.escape(str(name))}</b>'
        f'<span>{html.escape(str(text))}</span></div>'
        for index, (name, text) in enumerate(examples[:5])
    )
    active_class = " mode-processing" if processing else ""
    slot.markdown(
        f'<div class="chat-atmosphere {mode_class}{active_class}" aria-hidden="true">'
        '<div class="ambient-glow one"></div><div class="ambient-glow two"></div><div class="ambient-glow three"></div>'
        '<svg class="thread-lines" viewBox="0 0 1440 900" preserveAspectRatio="none">'
        '<defs><linearGradient id="conversation-gradient"><stop offset="0%" stop-color="#8f7cff"/><stop offset="52%" stop-color="#4be3d2"/><stop offset="100%" stop-color="#8f7cff"/></linearGradient></defs>'
        '<path d="M70 280 C260 180 310 460 510 345 S760 175 900 290 S1170 490 1390 330"/>'
        '<path d="M-20 690 C190 530 410 800 620 630 S930 440 1090 610 S1300 780 1480 570"/>'
        f'</svg><div class="background-scanner"></div>{bubbles}'
        '<div class="ambient-node n1"></div><div class="ambient-node n2"></div><div class="ambient-node n3"></div>'
        '<div class="orbit-ring ring-a"></div><div class="orbit-ring ring-b"></div></div>',
        unsafe_allow_html=True,
    )


def render_question_panel(report, model):
    """Render grounded follow-up chat and its source excerpts."""
    st.markdown("### 💬 Ask a question about this chat")
    st.caption(
        "Ask about the selected period. For tone or feelings, the assistant will separate what the messages say from what can only be inferred."
    )
    if not model:
        running, installed_models = engine.ollama_status()
        if running and installed_models:
            model = st.selectbox("Local model for questions", installed_models, key="qa_model_select")
            st.session_state["qa_model"] = model
        elif not running:
            st.info("Start Ollama and install a local model to ask questions about this chat.")
        else:
            st.info("Ollama is running, but no local model is installed yet. Install one, then rerun the analysis.")
    if not model:
        return

    history = st.session_state.setdefault("qa_history", [])
    question_count = sum(item.get("role") == "user" for item in history)
    if history:
        history_left, history_right = st.columns([3, 1])
        history_left.caption(f"{question_count} question{'s' if question_count != 1 else ''} in this session")
        if history_right.button("Clear history", key="qa_clear_history", use_container_width=True):
            st.session_state["qa_history"] = []
            st.rerun()
    for item in history[-12:]:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])
            if item["role"] == "assistant" and item.get("sources"):
                with st.expander(f"Evidence checked · {len(item['sources'])} messages"):
                    for source_no, source in enumerate(item["sources"], 1):
                        st.caption(f"Source {source_no} · {safe(source.get('who', 'Unknown'))} · {fmt(source['t'])}")
                        st.write(source.get("text", ""))
    if len(history) > 12:
        st.caption("Showing the most recent replies. Clear the history above to start fresh.")

    st.caption("Try one of these, or ask in your own words:")
    suggestions = [
        ("Read the tone", "How does the other person seem to feel in this conversation?"),
        ("Check engagement", "How engaged does the other person seem, based on their messages?"),
        ("Find a follow-up", "What would be a thoughtful thing to ask or follow up on?"),
    ]
    suggestion_columns = st.columns(3)
    for column, (label, value) in zip(suggestion_columns, suggestions):
        if column.button(label, key=f"qa_suggestion_{label}", use_container_width=True):
            st.session_state["qa_question_input"] = value
            st.rerun()

    with st.form("chat_question_form", clear_on_submit=True):
        question = st.text_input(
            "Your question", key="qa_question_input",
            placeholder="What plans did we make? Who mentioned the deadline?",
        )
        ask = st.form_submit_button("Ask about this chat", type="primary", use_container_width=True)
    if not ask or not question.strip():
        return

    history.append({"role": "user", "content": question.strip()})
    try:
        with st.spinner("Finding relevant messages and writing an answer…"):
            answer, sources = engine.answer_question(
                question.strip(), report.get("msgs", []), engine.LocalLLM(model), seen=report.get("seen")
            )
        history.append({"role": "assistant", "content": answer, "sources": sources})
    except Exception as exc:
        history.append({
            "role": "assistant",
            "content": f"I couldn’t answer because the local model returned an error: {exc}",
            "sources": [],
        })
    st.session_state["qa_history"] = history
    st.rerun()


def cutoff_for(option):
    now = datetime.now()
    if option == "Today":
        return datetime(now.year, now.month, now.day)
    if option == "This week":
        monday = now - timedelta(days=now.weekday())
        return datetime(monday.year, monday.month, monday.day)
    return None


def main():
    st.set_page_config(page_title="What Did I Miss?", page_icon="💬", layout="wide")
    st.markdown(f"<style>{Path(__file__).with_name('styles.css').read_text()}</style>", unsafe_allow_html=True)

    st.markdown(
        '<section class="hero"><div class="hero-copy">'
        '<div class="hero-kicker"><span class="kicker-dot"></span> YOUR CHAT COMPANION · CONVERSATION IN MOTION</div>'
        '<h1>Catch up without the scroll.</h1>'
        '<p>Follow the flow of a conversation. Find the moments, messages, and decisions that matter.</p>'
        '<div class="hero-points"><span>✦ Grounded summaries</span><span>◉ People and patterns</span><span>⌕ Ask follow-up questions</span></div>'
        '</div></section>',
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown("### 💬 What did I miss?")
        st.caption("Chats are processed by the computer or server hosting this app.")
        uploads = st.file_uploader(
            "Chat export", type=["zip", "txt", "json", "csv", "jpg", "jpeg", "png", "webp", "gif"],
            accept_multiple_files=True,
            help="Upload a WhatsApp export ZIP, a chat TXT/JSON/CSV file, or a chat file together with images.",
        )
        if st.button("Try the sample chat"):
            st.session_state["sample"] = True
        files = uploaded_files(uploads, st.session_state.get("sample", False))
        visual_mode = st.selectbox(
            "Background motion", ["Conversation flow", "Slow drift", "Off"],
            index=0, key="ambient_mode",
        )

    mode_class = {"Conversation flow": "mode-orbit", "Slow drift": "mode-calm", "Off": "mode-off"}[visual_mode]
    background_slot = st.empty()
    render_background(background_slot, mode_class)

    msgs, dmy, media, preview, people = [], True, {}, "", []
    problem = ""
    signature = content_signature(files) if files else None
    if st.session_state.get("loaded_signature") != signature:
        for key in ("report", "names", "me_label", "period_name", "ai_scope_label", "qa_model", "qa_history"):
            st.session_state.pop(key, None)
        st.session_state["loaded_signature"] = signature
    if files:
        try:
            msgs, dmy, media, preview, people = load_chat(files)
            if not msgs:
                problem = "No chat messages found in the upload. Check the file format or upload the WhatsApp export ZIP / chat text file."
            elif not people:
                problem = "Messages were read, but no participant names could be found."
        except Exception as exc:
            problem = f"Couldn't read the upload: {exc}"
            st.error(problem)

    if files and not problem:
        st.success(f"Found {len(msgs)} messages from {len(people)} people and {len(media)} images.")

        with st.sidebar:
            me = st.selectbox(
                "Which one are you? (choose manually)", people + [OTHER_ME], index=None,
                placeholder="Select your name", key=f"me_manual_v2_{signature}",
            )
            st.caption("The app will not guess which participant is you.")
            extra = ""
            if me:
                extra = st.text_input(
                    "Nicknames people use for you (optional)",
                    key=f"nick_{signature}", placeholder="e.g. Jay, JD",
                )

            relationship = None
            assess_relationship_dynamic = False
            if me in people and len(people) == 2:
                other_person = next((person for person in people if person != me), None)
                relation_options = ["Skip (optional)", "Friend", "Partner / spouse", "Family member", "Classmate", "Coworker", "Client / customer", "Other"]
                relation_choice = st.selectbox(
                    f"Your relationship with {other_person} (optional)", relation_options,
                    index=0, key=f"relationship_{signature}",
                )
                if relation_choice == "Other":
                    relationship = st.text_input("Relationship (optional)", key=f"relationship_other_{signature}").strip() or None
                elif relation_choice != "Skip (optional)":
                    relationship = relation_choice
                assess_relationship_dynamic = st.checkbox(
                    "Also assess whether the interaction seems mutual or one-sided (optional)",
                    value=False, key=f"relationship_dynamic_{signature}",
                    help="Uses only messages in the selected period. The result can be unclear and is not a definitive judgment about the relationship.",
                )

            timestamps = [m.get("t") for m in msgs if isinstance(m, dict) and isinstance(m.get("t"), datetime)]
            if not timestamps:
                st.error("The chat contains messages without valid timestamps, so it cannot be analyzed.")
                st.stop()
            min_date = min(timestamps).date()
            time_range = st.radio("Summarise / analyse messages from", ["Today", "This week", "All conversations", "Custom range"], index=2, key=f"range_{signature}")
            range_cutoff = cutoff_for(time_range)
            if time_range == "Custom range":
                custom_date = st.date_input("Custom start date", value=min_date, key=f"custom_{signature}")
                range_cutoff = datetime.combine(custom_date, time(0, 0))

            long_chat = len(timestamps) > 1 and (max(timestamps) - min(timestamps)).days > 7
            if st.checkbox("Only messages after I last checked", value=False, key=f"seen_{signature}"):
                default_last = max(timestamps).date() - timedelta(days=2) if long_chat else max(timestamps).date()
                last_date = st.date_input("Last checked date", value=default_last, key=f"last_date_{signature}")
                last_time = st.time_input("Last checked time", value=time(0, 0), key=f"last_time_{signature}")
                last_cutoff = datetime.combine(last_date, last_time)
            else:
                last_cutoff = None
            seen = max(x for x in (range_cutoff, last_cutoff) if x is not None) if range_cutoff or last_cutoff else None
            period_messages = [m for m in msgs if seen is None or m["t"] > seen]
            kept_count = len(period_messages)
            st.caption(f"Showing {kept_count} of {len(msgs)} messages" + (f" after {fmt(seen)}." if seen else " from all dates."))

            summary_scope = st.radio(
                "How much chat should the AI read?",
                ["Entire selected period", "Recent messages (faster)"],
                index=0, key=f"summary_scope_{signature}",
                help="The full period is the default. Message counts always include the whole selected period.",
            )

            running, models = engine.ollama_status()
            use_ai = st.toggle("Use local AI model", value=running)
            model, ai_limit, read_images, expected_calls = None, max(1, kept_count), False, 0
            if use_ai and running and models:
                suggested = engine.best_model(models)
                model = st.selectbox("Model", models, index=models.index(suggested) if suggested in models else 0)
                if summary_scope == "Recent messages (faster)":
                    ai_limit = int(st.number_input(
                        "Recent messages to include", min_value=30, max_value=3000,
                        value=min(60, max(30, kept_count)), step=30,
                        help="This limits the AI conclusion to the most recent messages. Choose Entire selected period for a complete summary.",
                    ))
                if media:
                    read_images = st.checkbox(
                        "Analyze attached images (slower)", value=False, key=f"read_images_{signature}",
                        help="Each image needs an additional local model call. Turn this on only when the images matter.",
                    )
                ai_messages = period_messages if summary_scope == "Entire selected period" else period_messages[-ai_limit:]
                chars = sum(min(1200, len(f"{m.get('who', '')}: {m.get('text', '')}")) + 35 for m in ai_messages)
                image_count = min(30, sum(1 for m in ai_messages if m.get("media") in media and str(m.get("media", "")).lower().endswith(IMAGE_EXTENSIONS))) if read_images else 0
                expected_calls = estimated_summary_calls(len(ai_messages), chars, image_count)
                learned_call_time = st.session_state.get(f"ollama_call_seconds_{model}")
                if learned_call_time:
                    estimate = duration_label(expected_calls * learned_call_time)
                    estimate_note = "based on your previous run with this model"
                else:
                    estimate = duration_label(expected_calls * 15)
                    estimate_note = "a rough first-run estimate; it will vary by Mac and model"
                st.caption(f"⏱️ Estimated analysis time: {estimate}. {estimate_note.capitalize()}.")
            if not running:
                st.warning("Ollama is not running, so this app cannot write a meaning-based chat conclusion yet. Install and start Ollama, pull a local model (for example, ollama pull gemma3:4b), then turn on ‘Use local AI model’. Sender counts and rule-based findings work without it.")
            elif not models:
                st.warning("Ollama is running but no local models are installed. In Terminal, run: ollama pull gemma3:4b")
            go = st.button("Find what I missed", type="primary", disabled=me is None)

        if go:
            listed = me != OTHER_ME
            names = engine.aliases_for(me if listed else "", extra) or []
            me_label = me if listed else (extra.split(",")[0].strip() or "you")
            try:
                llm = engine.LocalLLM(model) if use_ai and model else None
                analysis_started = clock_time.perf_counter()
                model_seconds = st.session_state.get(f"ollama_call_seconds_{model}") if model else None
                initial_seconds = max(5, expected_calls * (model_seconds or 15)) if llm else 5
                examples = processing_examples(period_messages)
                render_background(background_slot, mode_class, examples=examples, processing=True)
                work_panel = st.empty()
                show_work_status(
                    work_panel, 0.02, "Loading the messages in your selected period…",
                    elapsed=0, remaining=initial_seconds, examples=examples,
                )

                def update_work(progress_value, activity):
                    elapsed = clock_time.perf_counter() - analysis_started
                    fraction = min(0.99, max(0.02, float(progress_value)))
                    if elapsed >= 3 and fraction >= 0.08:
                        observed = elapsed * (1 - fraction) / fraction
                        projected = initial_seconds * (1 - fraction)
                        remaining = max(1, min(initial_seconds * 1.5, observed * 0.7 + projected * 0.3))
                    else:
                        remaining = initial_seconds * (1 - fraction)
                    show_work_status(
                        work_panel, progress_value, activity, elapsed=elapsed,
                        remaining=remaining, examples=examples,
                    )

                report = engine.analyze(
                    msgs, dmy, names, seen, llm,
                    progress=update_work,
                    media=media, me_senders=[me] if listed else [], ai_limit=ai_limit,
                    relationship=relationship,
                    assess_relationship_dynamic=assess_relationship_dynamic,
                    read_images=read_images,
                )
                elapsed = clock_time.perf_counter() - analysis_started
                show_work_status(
                    work_panel, 1.0, "Done", done=True, elapsed=elapsed, remaining=0, examples=examples,
                )
                render_background(background_slot, mode_class)
                if llm and expected_calls:
                    previous = st.session_state.get(f"ollama_call_seconds_{model}")
                    observed = elapsed / expected_calls
                    st.session_state[f"ollama_call_seconds_{model}"] = observed if not previous else (previous * 0.5 + observed * 0.5)
                if report is None:
                    st.error("The analyzer returned no report. Try uploading a chat with readable messages.")
                else:
                    period_name = time_range
                    if time_range == "Custom range":
                        period_name = f"from {custom_date.strftime('%d %b %Y')}"
                    st.session_state["report"] = report
                    st.session_state["names"] = names
                    st.session_state["me_label"] = me_label
                    st.session_state["period_name"] = period_name
                    st.session_state["qa_model"] = model if use_ai and model else None
                    st.session_state["qa_history"] = []
                    st.session_state["ai_scope_label"] = (
                        f"all {kept_count} messages in the selected period"
                        if summary_scope == "Entire selected period"
                        else f"the most recent {min(ai_limit, kept_count)} of {kept_count} messages"
                    )
            except Exception as exc:
                st.error(f"Analysis could not finish: {exc}")
    elif problem:
        st.error(problem)
        st.caption("Uploaded: " + ", ".join(name for name, _ in files))
        if preview:
            st.write("The chat file starts with:")
            st.code(preview)
        elif not files:
            st.write("Upload a WhatsApp export ZIP or a chat text file to begin.")

    report = st.session_state.get("report")
    if not report:
        st.markdown("""
        <div class="welcome">
          <div class="welcome-top">
            <div><h2>Your next catch-up starts here.</h2><p>A calmer way to find the important parts of a busy conversation.</p></div>
            <div class="status-pill">✧ LOCAL-FIRST · PRIVATE</div>
          </div>
          <div class="welcome-grid">
            <div class="welcome-card violet"><div class="welcome-icon">🫧</div><h3>Bring in a chat</h3><p>Drop a WhatsApp export or text file into the upload box. Your chat stays on this device.</p></div>
            <div class="welcome-card cyan"><div class="welcome-icon">🪄</div><h3>Choose your lens</h3><p>Pick a day, week, or custom range. Decide whether to include optional personal context.</p></div>
            <div class="welcome-card rose"><div class="welcome-icon">✨</div><h3>See the shape of it</h3><p>Get a grounded summary, message counts by sender, and a searchable conversation view.</p></div>
          </div>
          <div class="welcome-footer"><span>🔐</span><span><b>Choose where your chat is processed.</b> Run locally for local Ollama processing. A hosted deployment receives uploaded chats on its server.</span></div>
        </div>
        """, unsafe_allow_html=True)
        return

    names = st.session_state.get("names", [])
    ai = report.get("ai") or {}
    period_name = st.session_state.get("period_name", "selected period")
    st.caption(f"Catch-up for {st.session_state.get('me_label', 'you')} · {period_name}")
    if ai.get("tldr"):
        st.caption(f"AI coverage: {st.session_state.get('ai_scope_label', 'selected messages')}")
    overview_tab, highlights_tab, timeline_tab, chat_tab = st.tabs(
        ["✨ Overview", "📌 Highlights", "🗓️ Timeline", "💬 Explore chat"]
    )
    with overview_tab:
        st.markdown(
            f'<div class="dashboard-heading"><div><h2>{safe(period_name)} at a glance</h2>'
            '<p>A clear read on the conversation, with the people and patterns behind it.</p></div>'
            f'<div class="period-badge">✦ {report.get("n_unread", 0):,} MESSAGES</div></div>',
            unsafe_allow_html=True,
        )
        if ai.get("tldr"):
            conclusion_body = summary_html(ai["tldr"])
            conclusion_tag = "LOCAL AI SUMMARY"
        else:
            conclusion_body = '<p class="summary-paragraph">No meaning-based conclusion was generated. Start Ollama, enable the local model, and run the analysis again to get a narrative summary.</p>'
            conclusion_tag = "SUMMARY UNAVAILABLE"
        st.markdown(
            '<div class="conclusion-card"><div class="conclusion-head">'
            '<div class="conclusion-icon">✦</div><div class="conclusion-title">Conversation conclusion</div>'
            f'<div class="conclusion-tag">{conclusion_tag}</div></div>{conclusion_body}</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            render_question_panel(report, st.session_state.get("qa_model"))

        sender_counts = report.get("sender_counts") or []
        left, right = st.columns([1.15, .85], gap="large")
        with left:
            st.markdown(
                '<div class="section-eyebrow">PARTICIPATION</div><h3 style="margin-top:0">Who’s in the conversation</h3>'
                '<p style="color:rgba(218,228,249,.62);margin-top:-8px">Message share across everyone in this period.</p>',
                unsafe_allow_html=True,
            )
            st.markdown(f'<div class="sender-card">{sender_bars(sender_counts)}</div>', unsafe_allow_html=True)
        with right:
            st.markdown(
                '<div class="section-eyebrow">AT A GLANCE</div><h3 style="margin-top:0">Your chat pulse</h3>',
                unsafe_allow_html=True,
            )
            metrics_a = st.columns(2)
            metrics_a[0].metric("Messages", report.get("n_unread", 0))
            metrics_a[1].metric("People", report.get("people", 0))
            metrics_b = st.columns(2)
            metrics_b[0].metric("For you", len(report.get("needs") or []))
            metrics_b[1].metric("Possibly missed", len(report.get("missed") or []))
            st.caption(f"{len(report.get('ahead') or [])} upcoming · {len(report.get('decisions') or [])} decisions · flags are helpful hints, not a complete reading of the conversation.")
        if ai.get("images"):
            st.caption(f"🖼️ {ai['images']} image(s) read by the local model and included in the analysis.")
        if ai.get("errors"):
            with st.expander("AI notes"):
                st.text("\n".join(map(str, ai["errors"]))[:2500])

    with highlights_tab:
        for title, key in (("⚠️ Possibly missed", "missed"), ("📌 Needs you", "needs")):
            items = report.get(key) or []
            st.subheader(f"{title} · {len(items)}")
            if not items:
                st.caption("Nothing matched the rules for this section in the selected period.")
            else:
                for message in items[:LIMIT]:
                    render_card(message, report, names)
                if len(items) > LIMIT:
                    st.caption(f"Showing the top {LIMIT}. Download the report to see the rest.")

    with timeline_tab:
        for title, key in (("⏰ Coming up", "ahead"), ("✅ Decisions made", "decisions")):
            items = report.get(key) or []
            st.subheader(f"{title} · {len(items)}")
            if not items:
                st.caption("Nothing matched the rules for this section in the selected period.")
            else:
                render_rows(items[:LIMIT * 2], report)
                if len(items) > LIMIT * 2:
                    st.caption(f"Showing the top {LIMIT * 2}. Download the report to see the rest.")

    with chat_tab:
        visible = [m for m in report.get("msgs", []) if report.get("seen") is None or m["t"] > report["seen"]]
        senders = ["Everyone"] + list(dict.fromkeys(m.get("who", "Unknown") for m in visible))
        left, right = st.columns([1, 2])
        with left:
            selected_sender = st.selectbox("Filter by sender", senders, key="chat_sender_filter")
        with right:
            search = st.text_input("Search messages", placeholder="Type a word or phrase…", key="chat_text_filter")
        if selected_sender != "Everyone":
            visible = [m for m in visible if m.get("who") == selected_sender]
        if search.strip():
            visible = [m for m in visible if search.casefold() in m.get("text", "").casefold()]
        st.caption(f"Showing {len(visible)} message(s)")
        for message in visible:
            st.markdown(f"**{safe(message.get('who', 'Unknown'))}** · {fmt(message['t'])}")
            st.write(message.get("text", ""))
            st.divider()

    st.download_button("⬇️ Download your catch-up (.md)", engine.to_markdown(report), "what-did-i-miss.md")


if __name__ == "__main__":
    main()
