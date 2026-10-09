"""What Did I Miss? - local-first chat triage engine.

Pipeline: parse -> rule signals -> (optional) local LLM map/reduce via Ollama -> score -> report.
Nothing leaves the machine: the LLM host is forced to be localhost.
"""
from __future__ import annotations
import csv, io, json, re, urllib.error, urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse

# ---------------------------------------------------------------- parsing
LINE = re.compile(
    r"^\[?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4}),?\s+(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?\]?\s*(?:-\s*)?([^:\n]{1,40}?):\s(.*)$"
)
ISO = re.compile(
    r"^\[?(\d{4})-(\d{2})-(\d{2})[ ,T]+(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp]\.?[Mm]\.?)?\]?\s*(?:-\s*)?([^:\n]{1,40}?):\s(.*)$"
)
DATEONLY = re.compile(r"^\[?(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}-\d{2}-\d{2}),?\s+\d{1,2}[:.]\d{2}")
MEDIA = re.compile(r"^(<media omitted>|this message was deleted|you deleted this message|image omitted)$", re.I)
ATT = re.compile(r"(?:<attached:\s*([^>]+?)\s*>|([^\s<>:]+\.(?:jpe?g|png|webp|gif|heic))\s*\(file attached\))", re.I)
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _hour(h, ap):
    hh = int(h)
    if ap:
        pm = "p" in ap.lower()
        hh = hh + 12 if pm and hh < 12 else (0 if not pm and hh == 12 else hh)
    return hh


def parse_whatsapp(raw: str):
    lines = re.sub(r"[\u200e\u200f\ufeff]", "", raw).replace("\u202f", " ").replace("\u00a0", " ").splitlines()
    a = b = 0
    for l in lines:
        m = LINE.match(l)
        if m:
            a += int(m[1]) > 12
            b += int(m[2]) > 12
    dmy = not (b > 0 and a == 0)  # default day-first (India)
    msgs = []
    for l in lines:
        t = None
        if m := ISO.match(l):
            y, mo, d, h, mi, ap, who, txt = m.groups()
            try:
                t = datetime(int(y), int(mo), int(d), _hour(h, ap), int(mi))
            except ValueError:
                t = None
        elif m := LINE.match(l):
            x, y, yr, h, mi, ap, who, txt = m.groups()
            yr = int(yr) + 2000 if int(yr) < 100 else int(yr)
            day, mon = (int(x), int(y)) if dmy else (int(y), int(x))
            try:
                t = datetime(yr, mon, day, _hour(h, ap), int(mi))
            except ValueError:
                t = None
        if t:
            msgs.append({"who": who.strip(), "text": txt, "t": t})
        elif DATEONLY.match(l):
            continue  # system line
        elif msgs and l.strip():
            msgs[-1]["text"] += "\n" + l
    msgs = [m for m in msgs if not MEDIA.match(m["text"].strip())]
    for m in msgs:
        a_ = ATT.search(m["text"])
        m["media"] = (a_.group(1) or a_.group(2)).strip() if a_ else None
    return msgs, dmy


def _parse_other(name: str, text: str):
    n = name.lower()
    if n.endswith(".json"):
        j = json.loads(text)
        items = j.get("messages", j) if isinstance(j, dict) else j
        out = []
        for it in items:
            body = it.get("text") or it.get("message") or ""
            if isinstance(body, list):
                body = "".join(p if isinstance(p, str) else p.get("text", "") for p in body)
            who = it.get("from") or it.get("sender") or it.get("user") or "?"
            ts = it.get("date") or it.get("time") or it.get("timestamp")
            if body and ts:
                out.append({"who": str(who), "text": body, "t": datetime.fromisoformat(str(ts).replace("Z", "")), "media": it.get("photo") and str(it["photo"]).split("/")[-1]})
        return out, True
    if n.endswith(".csv"):
        out = []
        for r in csv.DictReader(io.StringIO(text)):
            r = {k.lower().strip(): v for k, v in r.items()}
            ts = r.get("time") or r.get("date") or r.get("timestamp")
            out.append({"who": r.get("sender") or r.get("from") or "?", "text": r.get("message") or r.get("text") or "",
                        "t": datetime.fromisoformat(ts), "media": None})
        return out, True
    return parse_whatsapp(text)


def load_files(files):
    """files: [(name, bytes)]. Accepts a WhatsApp export ZIP (even if renamed .txt), .txt/.json/.csv chats,
    and image files. Returns (msgs, dmy, media{basename: bytes}, preview_text_for_errors)."""
    import zipfile
    media, chats = {}, []

    def add(name, data, depth=0):
        base = name.replace("\\", "/").split("/")[-1]
        if data[:4] == b"PK\x03\x04" and depth < 2:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for zi in z.infolist():
                        if zi.is_dir() or "__MACOSX" in zi.filename or zi.file_size > 200_000_000:
                            continue
                        add(zi.filename, z.read(zi), depth + 1)
            except zipfile.BadZipFile:
                pass
        elif base.lower().endswith(IMG_EXT):
            media[base] = data
        elif base.lower().endswith((".txt", ".json", ".csv")):
            chats.append((base, data))

    for n, d in files:
        add(n, d)
    chats.sort(key=lambda c: (c[0].lower() != "_chat.txt", -len(c[1])))
    preview = ""
    for name, data in chats:
        text = data.decode("utf-8-sig", errors="ignore")
        preview = preview or "\n".join(text.splitlines()[:4])[:400]
        try:
            msgs, dmy = _parse_other(name, text)
        except Exception:
            continue
        if msgs:
            return msgs, dmy, media, preview
    return [], True, media, preview


def parse_any(name: str, data: bytes):  # kept for simple use
    msgs, dmy, _, _ = load_files([(name, data)])
    return msgs, dmy


# ---------------------------------------------------------------- rule signals
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # python weekday(): Mon=0
URG = re.compile(r"\b(urgent|asap|immediately|important|critical|deadline|last date|final call|emergency|priority|mandatory|compulsory|no extensions?|right now|reminder|don'?t forget|must|confirm)\b", re.I)
ACT = re.compile(r"\b(please|pls|kindly|can you|could you|need to|needs to|have to|must|should|make sure|ensure|submit|send|share|upload|prepare|complete|finish|review|update|fix|check|confirm|bring|book|register|reply|respond|join|attend|be ready|todo)\b", re.I)
DEC = re.compile(r"\b(decided|finali[sz]ed|we(?:'ll| will) go with|let'?s go with|going with|agreed|approved|confirmed|rescheduled|postponed|cancel+ed|moved to|changed to|it'?s final|final decision|is final|, final)\b", re.I)
BROAD = re.compile(r"\b(everyone|everybody|all|team|guys|folks)\b", re.I)
CUE = re.compile(r"\b(by|before|due|deadline|submit|until|till|meeting|meet|call|exam|demo|viva|presentation|review|send|complete|finish|ready|eod|join|reach|at)\b", re.I)
ACK = re.compile(r"\b(done|sent|submitted|uploaded|completed|will do|on it|ok|okay|sure|noted|got it|yes|yep|finished|fixed)\b", re.I)


def parse_deadline(text: str, base: datetime, dmy: bool = True):
    """Never raises: real chats contain things like '31 feb', '99/99' or 'at 25:70'."""
    try:
        return _parse_deadline(text, base, dmy)
    except (ValueError, OverflowError, IndexError):
        return None


def _parse_deadline(text: str, base: datetime, dmy: bool = True):
    l = text.lower()
    d = None
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*", l):
        d = datetime(base.year, MONTHS.index(m[2]) + 1, int(m[1]))
    elif m := re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", l):
        day, mon = (int(m[1]), int(m[2])) if dmy else (int(m[2]), int(m[1]))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            y = base.year if not m[3] else (2000 + int(m[3]) if int(m[3]) < 100 else int(m[3]))
            try:
                d = datetime(y, mon, day)
            except ValueError:
                pass
    elif "day after tomorrow" in l:
        d = base + timedelta(days=2)
    elif re.search(r"\btomorrow\b", l):
        d = base + timedelta(days=1)
    elif re.search(r"\b(today|tonight|eod|end of (the )?day)\b", l):
        d = base
    elif m := re.search(r"\b(?:next |this )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", l):
        diff = (DAYS.index(m[1][:3]) - base.weekday()) % 7 or 7
        d = base + timedelta(days=diff)
    elif re.search(r"\b(eow|end of (the )?week)\b", l):
        d = base + timedelta(days=(4 - base.weekday()) % 7 or 7)
    h, mi = None, 0
    if m := re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", l):
        h, mi = int(m[1]) % 12 + (12 if m[3] == "pm" else 0), int(m[2] or 0)
    elif m := re.search(r"\bat (\d{1,2}):(\d{2})\b", l):
        h, mi = int(m[1]), int(m[2])
    elif "noon" in l: h = 12
    elif "evening" in l: h = 18
    elif "morning" in l: h = 10
    elif "tonight" in l: h = 21
    if d is None and h is not None and re.search(r"\b(at|by|before)\b", l):
        d = base
    if d is None:
        return None
    return d.replace(hour=h if h is not None else 23, minute=mi if h is not None else 59, second=0, microsecond=0)


def add_signals(msgs, names, dmy, me_senders=None):
    ref = msgs[-1]["t"]
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    me_re = re.compile(rf"(?<!\w)@?({alt})(?!\w)", re.I) if alt else None
    me_set = {x.strip().lower() for x in (me_senders or names)} | {"you"}
    is_me = lambda w: w.strip().lower() in me_set
    for k, m in enumerate(msgs):
        m["i"] = k
        m["from_me"] = is_me(m["who"])
    for k, m in enumerate(msgs):
        m["mention"] = bool(me_re and not m["from_me"] and me_re.search(m["text"]))
        m["urgent"] = bool(URG.search(m["text"]))
        m["broad"] = bool(BROAD.search(m["text"]))
        m["action"] = bool(ACT.search(m["text"]))
        m["decision"] = bool(DEC.search(m["text"]))
        m["question"] = "?" in m["text"] and m["mention"]
        clean = ATT.sub(" ", m["text"])  # filenames contain dates; don't read them as deadlines
        m["deadline"] = parse_deadline(clean[:2000], m["t"], dmy) if CUE.search(clean) else None
        m["overdue"] = bool(m["deadline"] and m["deadline"] < ref)
        m["handled"] = any(msgs[j]["from_me"] and ACK.search(msgs[j]["text"]) for j in range(k + 1, min(len(msgs), k + 9)))
        m["llm_action"] = m["llm_important"] = m["llm_decision"] = False
        m["llm_task"] = None
        m.setdefault("media", None)
    return ref


def score(m, ref):
    if m["from_me"]:
        return 0.0
    prox = 0.0
    if m["deadline"]:
        h = (m["deadline"] - ref).total_seconds() / 3600
        prox = 1 if h <= 24 else .7 if h <= 72 else .4 if h <= 168 else .2
    relevant = m["mention"] or m["broad"]
    s = (.35 * m["mention"] + .10 * m["question"] + .30 * prox * (1 if relevant else .5)
         + .20 * m["urgent"] + .15 * ((m["action"] and relevant) or m["decision"])
         + .15 * m["llm_action"] + .10 * m["llm_important"] + (.05 if m["llm_decision"] else 0))
    return min(1.0, s)


def reasons(m):
    r = []
    if m["mention"]: r.append("mentions you")
    if m["question"]: r.append("asks you a question")
    if m["deadline"]: r.append("deadline passed" if m["overdue"] else "deadline")
    if m["urgent"]: r.append("urgent wording")
    if m["decision"] or m["llm_decision"]: r.append("decision")
    if m["llm_action"]: r.append("AI: action for you")
    elif m["action"] and (m["mention"] or m["broad"]): r.append("action")
    if m["llm_important"]: r.append("AI: important")
    if m.get("caption"): r.append("image analysed")
    return r


# ---------------------------------------------------------------- local LLM (Ollama)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def ollama_status(host="http://localhost:11434"):
    """Returns (running, [installed model names]). Never raises."""
    try:
        if urlparse(host).hostname not in LOCAL_HOSTS:
            return False, []
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=3) as r:
            return True, [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return False, []


def best_model(models):
    for pref in ("gemma3:4b", "gemma3", "llama3.2-vision", "qwen2.5vl", "gemma", "qwen", "llama"):
        for m in models:
            if pref in m.lower():
                return m
    return models[0] if models else None


class LocalLLM:
    _vision = None

    def __init__(self, model="gemma3:4b", host="http://localhost:11434", timeout=300):
        if urlparse(host).hostname not in LOCAL_HOSTS:
            raise ValueError("Privacy guard: LLM host must be localhost. Data never leaves the device.")
        self.model, self.host, self.timeout = model, host.rstrip("/"), timeout

    def _post(self, path, body, timeout=None):
        req = urllib.request.Request(self.host + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.load(e).get("error", "")
            except Exception:
                pass
            raise RuntimeError(f"Ollama error {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Can't reach Ollama at {self.host} ({e.reason})") from None
        except OSError as e:
            raise RuntimeError(f"Ollama timed out or failed: {e}") from None

    def chat(self, system, user, as_json=False, images=None):
        import base64
        u = {"role": "user", "content": user}
        if images:
            u["images"] = [base64.b64encode(i).decode() for i in images]
        body = {"model": self.model, "stream": False, "messages": [{"role": "system", "content": system}, u],
                "options": {"temperature": 0.1, "num_ctx": 8192}}
        if as_json:
            body["format"] = "json"
        return self._post("/api/chat", body)["message"]["content"]

    def vision(self):
        if self._vision is None:
            try:
                caps = self._post("/api/show", {"model": self.model}, 10).get("capabilities")
            except Exception:
                caps = None
            if caps is not None:
                self._vision = "vision" in caps
            else:
                n = self.model.lower()
                self._vision = any(k in n for k in ("gemma3", "llava", "vision", "-vl", "vl:", "minicpm-v", "moondream")) and not re.search(r":(1b|270m)\b", n)
        return self._vision


MAP_SYS = """You analyse chat messages for one reader. Messages look like [id] time | sender: text.
Return ONLY JSON with this shape:
{"summary":"2-3 sentences","decisions":[{"text":"...","ids":[1]}],
"action_items":[{"task":"...","owner":"person name or everyone","deadline":"as written or null","urgency":"high|medium|low","ids":[1]}],
"important_ids":[1]}
Rules:
- Use only facts explicitly supported by the messages. Never invent a decision, request, action, deadline, location, or intention.
- Do not guess what abbreviations, initials, slang, nicknames, or shorthand mean. Keep the exact original wording and say it is unclear when needed.
- A decision must be explicitly agreed or stated as final. An action item must be an explicit request or commitment. Use null for a deadline unless one is stated.
- If there is no supported item, return an empty list. ids must be message ids from this chunk.
- If a task is explicitly for the reader, set owner to the reader's supplied name."""

CAPTION_PROMPT = ("Describe this image from a chat in 1-3 sentences. If it contains text (notice, timetable, poster, assignment, "
                  "screenshot), transcribe the key text exactly, especially dates, times, names and deadlines. No preamble.")


def _prep_image(data):
    try:  # shrink big photos so the local model is fast; Pillow is optional
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return data


def caption_images(msgs, media, llm, progress=None, limit=30):
    """Describe/OCR images with a local vision model and append the result to the message text."""
    todo = [m for m in msgs if m.get("media") in media and m["media"].lower().endswith(IMG_EXT)][-limit:]
    if not todo:
        return 0, []
    if not llm.vision():
        return 0, [f"Model {llm.model} can't read images, so {len(todo)} image(s) were skipped. Use gemma3:4b or a larger vision model."]
    errors = []
    for n, m in enumerate(todo):
        if progress: progress(n / max(1, len(todo)), f"Looking at image {n + 1} of {len(todo)}")
        try:
            cap = llm.chat("You describe images from a chat for a busy reader.", CAPTION_PROMPT, images=[_prep_image(media[m["media"]])]).strip()
            if cap:
                m["caption"] = cap
                m["text"] += f"\n[Image: {cap}]"
        except Exception as e:
            errors.append(f"{m['media']}: {e}")
            if len(errors) >= 2:
                errors.append("Stopped image analysis after repeated errors.")
                break
    return len([m for m in todo if m.get("caption")]), errors


def chunk(msgs, max_msgs=40, max_chars=3500):
    cur, size = [], 0
    for m in msgs:
        line = f"[{m['i']}] {m['t']:%d %b %H:%M} | {m['who']}: {m['text']}"[:1200]
        if cur and (len(cur) >= max_msgs or size + len(line) > max_chars):
            yield cur
            cur, size = [], 0
        cur.append((m, line))
        size += len(line)
    if cur:
        yield cur


def _loads_json(t):
    try:
        return json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


def _ids(x):
    out = []
    for v in (x if isinstance(x, list) else [x]):
        try:
            out.append(int(str(v).strip("[]# ")))
        except Exception:
            pass
    return out


def llm_pass(msgs, names, llm: LocalLLM, progress=None, relationship=None):
    """Map over chunks, merge AI findings onto messages, return (chunk_summaries, action_items, errors)."""
    by_id = {m["i"]: m for m in msgs}
    me = ", ".join(names[:3]) or "the reader"
    summaries, items, errors = [], [], []
    chunks = list(chunk(msgs))
    fails = 0
    for n, ch in enumerate(chunks):
        if progress: progress(n / max(1, len(chunks)), f"Reading part {n + 1} of {len(chunks)}")
        text = "\n".join(l for _, l in ch)
        data, err = None, ""
        for _ in range(2):  # one retry on bad output
            try:
                context = f"\nRelationship context supplied by the user: {relationship}. Use only if it helps with tone; do not infer other personal details." if relationship else ""
                data = _loads_json(llm.chat(MAP_SYS, f"Reader: {me}{context}\n\nChat:\n{text}", as_json=True))
                if isinstance(data, dict):
                    break
                data = None
            except Exception as e:
                err = str(e)
                if "Can't reach" in err or "Ollama error 404" in err:
                    break  # retrying won't help
        if data is None:
            errors.append(f"part {n + 1}: {err or 'unreadable model output'}")
            fails += 1
            if fails >= 2 and not summaries:
                errors.append("Stopped AI analysis after repeated errors.")
                break
            continue
        summaries.append(str(data.get("summary", "")))
        for i in _ids(data.get("important_ids")):
            if i in by_id: by_id[i]["llm_important"] = True
        for d in data.get("decisions") or []:
            if isinstance(d, dict):
                for i in _ids(d.get("ids")):
                    if i in by_id: by_id[i]["llm_decision"] = True
        for a in data.get("action_items") or []:
            if not isinstance(a, dict):
                continue
            owner = str(a.get("owner") or "").lower()
            mine = owner in ("everyone", "all", "team", "you", "me", "anyone") or any(x.lower() in owner for x in names if x)
            item = {"task": str(a.get("task") or ""), "owner": owner, "deadline": a.get("deadline"),
                    "urgency": str(a.get("urgency") or "").lower(), "ids": _ids(a.get("ids")), "mine": mine}
            items.append(item)
            if mine:
                for i in item["ids"]:
                    if i in by_id:
                        by_id[i]["llm_action"] = True
                        by_id[i]["llm_task"] = item["task"]
                        if item["urgency"] == "high": by_id[i]["urgent"] = True
    if progress: progress(.8, "Preparing the conversation conclusion")
    return summaries, items, errors


def llm_tldr(llm, names, summaries, report_hint, source_messages="", relationship=None, assess_relationship_dynamic=False):
    sys = ("Summarize this chat for a busy reader in at most 5 short bullets. Separate what happened, explicit decisions, "
           "and actions/deadlines. Use only the supplied messages. Never guess meanings of abbreviations, initials, slang, "
           "or nicknames; preserve the exact wording and mark it unclear. Do not invent a decision, location, deadline, "
           "or action. If nothing is explicit, say so. Plain and specific.")
    if assess_relationship_dynamic:
        sys += (" Because the reader opted in, add a separate short section titled 'Interaction pattern (uncertain)'. "
                "Assess only whether the visible communication/initiative in these messages appears mutual, mostly from the reader, "
                "mostly from the other person, or unclear. Support it with concrete examples from the supplied messages. "
                "Do not infer love, motives, personality, or the true nature/quality of the relationship. A short or filtered period "
                "may not be representative; say 'unclear from this period' when evidence is insufficient.")
    context = f"\nUser-supplied relationship context: {relationship}. Do not infer personal details from it." if relationship else ""
    compact_summaries = list(summaries)
    reduce_system = (
        "Combine these notes from different parts of the same chat into a concise set of factual notes. "
        "Preserve separate topics and explicit decisions/actions. Do not add interpretations or expand ambiguous shorthand. "
        "Return plain text only."
    )
    while len(compact_summaries) > 12:
        compact = []
        for start in range(0, len(compact_summaries), 12):
            group = compact_summaries[start:start + 12]
            compact.append(llm.chat(reduce_system, "Chat notes:\n" + "\n".join(group)))
        compact_summaries = compact
    user = (f"Reader: {', '.join(names[:3]) or 'not specified'}{context}\n\nOriginal messages (primary evidence):\n{source_messages}"
            f"\n\nChunk notes covering the selected period (secondary; ignore any claim that conflicts with original messages):\n" + "\n".join(compact_summaries)
            + "\n\nRule-based findings (may be empty):\n" + report_hint)
    return llm.chat(sys, user)


QA_STOP_WORDS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how", "did", "does", "do", "is", "are",
    "was", "were", "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "from", "with",
    "about", "this", "that", "these", "those", "it", "they", "them", "we", "you", "i", "me", "my", "our", "their",
    "tell", "please", "can", "could", "would", "should", "explain", "summarize", "chat", "messages", "message",
    "much", "other", "person", "someone", "someone's", "their", "they're", "really", "seem", "seems",
}


def retrieve_question_messages(question, msgs, seen=None, limit=12, max_context=24):
    """Lightweight local keyword retrieval for chat follow-up questions."""
    terms = [t for t in re.findall(r"[\w@+.-]+", question.lower()) if t not in QA_STOP_WORDS and len(t) >= 2]
    if not terms:
        return []
    expansions = {
        "decide": {"decision", "decided", "agree", "agreed", "final", "approved", "choose", "chose"},
        "decision": {"decide", "decided", "agree", "agreed", "final", "approved", "choose", "chose"},
        "plan": {"planned", "planning", "schedule", "meeting", "arrange"},
        "deadline": {"due", "before", "submit", "submission", "tomorrow", "date"},
        "feel": {"feeling", "happy", "sad", "excited", "upset", "worried", "miss", "love", "like", "care", "sorry", "angry", "tired", "stress", "fun", "😊", "❤️"},
        "feeling": {"feel", "happy", "sad", "excited", "upset", "worried", "miss", "love", "like", "care", "sorry", "angry", "tired", "stress", "fun"},
        "interested": {"interest", "want", "like", "love", "miss", "meet", "call", "talk", "date", "together", "question", "asked", "ask", "initiate", "plan", "reply"},
        "interest": {"interested", "want", "like", "love", "miss", "meet", "call", "talk", "date", "together", "question", "asked", "ask", "initiate", "plan", "reply"},
        "like": {"love", "want", "miss", "meet", "call", "talk", "together", "care", "😊", "❤️"},
        "tone": {"feel", "happy", "sad", "excited", "upset", "worried", "angry", "sorry", "thanks", "please", "haha", "lol"},
    }
    expanded = set(terms)
    for term in terms:
        expanded.update(expansions.get(term, set()))
    asks_about_emotions = any(x in question.lower() for x in ("feel", "emotion", "mood", "tone", "upset", "happy", "sad", "angry"))
    asks_about_interest = any(x in question.lower() for x in ("interest", "interested", "like me", "attracted", "engaged", "care about me", "feel about me"))
    asks_about_user = bool(re.search(r"\b(i|me|my|myself)\b", question.lower()))
    emotion_cues = {"feel", "feeling", "happy", "sad", "excited", "upset", "worried", "miss", "love", "like", "care", "sorry", "angry", "tired", "stress", "confused", "frustrated", "lonely", "😊", "😢", "😔", "❤️"}
    engagement_cues = {"ask", "asked", "question", "want", "like", "love", "miss", "meet", "call", "talk", "date", "together", "plan", "reply", "tomorrow", "when", "where"}
    ranked = []
    normalized_question = re.sub(r"\W+", " ", question.lower()).strip()
    for msg in msgs:
        if seen is not None and msg["t"] <= seen:
            continue
        text = f"{msg.get('who', '')} {msg.get('text', '')}".lower()
        msg_terms = set(re.findall(r"[\w@+.-]+", text))
        score = sum(2 if term in msg_terms else 0 for term in terms)
        score += sum(1 for term in expanded - set(terms) if term in msg_terms)
        # For broad emotional/relationship questions, search the relevant person's
        # messages for evidence even when the question's wording is absent verbatim.
        if asks_about_emotions:
            score += sum(2 for cue in emotion_cues if cue in msg_terms)
            if asks_about_user and msg.get("from_me"):
                score += 2
        if asks_about_interest:
            score += sum(1 for cue in engagement_cues if cue in msg_terms)
            if not asks_about_user and not msg.get("from_me"):
                score += 1
        normalized_text = re.sub(r"\W+", " ", text)
        if len(normalized_question) >= 4 and normalized_question in normalized_text:
            score += 8
        if score:
            ranked.append((score, msg))
    ranked.sort(key=lambda pair: (pair[0], pair[1]["t"]), reverse=True)
    core = [msg for _, msg in ranked[:limit]]
    if not core:
        return []
    by_id = {m.get("i", i): m for i, m in enumerate(msgs) if seen is None or m["t"] > seen}
    chosen = {m.get("i", -1) for m in core}
    for msg in core:
        idx = msg.get("i", -1)
        for neighbor in (by_id.get(idx - 1), by_id.get(idx + 1)):
            if neighbor and len(chosen) < max_context:
                chosen.add(neighbor.get("i", -1))
    return sorted((by_id[i] for i in chosen if i in by_id), key=lambda m: m["t"])


def answer_question(question, msgs, llm, seen=None):
    """Answer from retrieved chat excerpts and return the exact source messages used."""
    sources = retrieve_question_messages(question, msgs, seen=seen)
    if not sources:
        return ("I couldn’t find a matching message in the selected period. Try asking with a person’s name, an exact phrase, or a topic from the chat."), []
    system = (
        "Answer using only the supplied chat excerpts. Be conversational and usually keep the answer to 2-5 sentences. Cite no more than four "
        "supporting excerpts with their short reference numbers, like [1] or [2]. If evidence is insufficient, say so directly. The user may ask "
        "for a read of someone's tone, feelings, intentions, or interest: give a tentative interpretation only when the messages support it, name "
        "the specific behavior or words, and distinguish direct evidence from inference. Text cannot establish someone's inner state. Do not "
        "claim attraction from an emoji, photo, delayed reply, or one isolated message alone; mention plausible alternative explanations. Never "
        "invent details, assign a numeric interest score, or guess what abbreviations, initials, slang, or nicknames mean. Optional relationship "
        "context is only a lens, not proof."
    )
    lines = []
    for source_no, msg in enumerate(sources, 1):
        lines.append(f"[{source_no}] {msg['t']:%d %b %Y %H:%M} | {msg.get('who', 'Unknown')}: {str(msg.get('text', ''))[:800]}")
    user = f"Question: {question}\n\nRelevant messages from the selected period:\n" + "\n".join(lines)
    return llm.chat(system, user), sources


# ---------------------------------------------------------------- who is who
SYSTEM_WHO = re.compile(r"\b(created|added|removed|left|joined|changed|security code|end-to-end)\b", re.I)


def participants(msgs):
    counts = {}
    for m in msgs:
        w = m["who"].strip()
        if w and not SYSTEM_WHO.search(w):
            counts[w] = counts.get(w, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def aliases_for(name, extra=""):
    """Names people may use to address this person: full sender name, each word, phone digits, typed nicknames."""
    out = [name.strip()] if name.strip() else []
    out += [w for w in re.split(r"[\s,]+", name) if len(w) >= 3 and not w.isdigit() and not w.startswith("+")]
    digits = re.sub(r"\D", "", name)
    if len(digits) >= 8:
        out.append(digits)
    out += [e.strip() for e in (extra or "").split(",") if e.strip()]
    return list(dict.fromkeys(out))


def guess_me(msgs):
    """Best guess for the reader: the participant most often addressed by name in other people's messages."""
    people = [p for p, _ in participants(msgs)]
    if not people:
        return None
    best, top = people[0], 0
    for p in people:
        toks = [w for w in re.split(r"\s+", p) if len(w) >= 3 and not w.startswith("+")]
        if not toks:
            continue
        rx = re.compile(r"(?<!\w)@?(" + "|".join(re.escape(t) for t in toks) + r")(?!\w)", re.I)
        c = sum(1 for m in msgs if m["who"] != p and rx.search(m["text"]))
        if c > top:
            best, top = p, c
    return best


# ---------------------------------------------------------------- orchestration
def analyze(msgs, dmy, names, seen=None, llm: LocalLLM | None = None, progress=None, media=None, me_senders=None, ai_limit=60, relationship=None, assess_relationship_dynamic=False, read_images=False):
    if not msgs:
        return None
    msgs = sorted(msgs, key=lambda m: m["t"])
    media = media or {}
    ai = {"used": False, "summaries": [], "items": [], "errors": [], "tldr": None, "images": 0}
    sp = (lambda f: (lambda p, t: progress(f[0] + p * f[1], t))) if progress else (lambda f: None)
    if llm and media and read_images:
        try:
            ai["images"], er = caption_images([m for m in msgs if not seen or m["t"] > seen], media, llm, sp((0, .4)))
            ai["errors"] += er
        except Exception as e:
            ai["errors"].append(str(e))
    ref = add_signals(msgs, names, dmy, me_senders)
    unread_ids = {m["i"] for m in msgs if not seen or m["t"] > seen}
    if llm:
        unread = [m for m in msgs if m["i"] in unread_ids][-max(1, int(ai_limit)):]
        try:
            s, it, er = llm_pass(unread, names, llm, sp((.4, .6)), relationship)
            ai.update(used=bool(s), summaries=s, items=it)
            ai["errors"] += er
        except Exception as e:  # Ollama down etc: fall back to rules only
            ai["errors"].append(str(e))
    for m in msgs:
        m["score"] = score(m, ref)
        m["bucket"] = "high" if m["score"] >= .5 else "med" if m["score"] >= .25 else "low"
        m["reasons"] = reasons(m)
    U = [m for m in msgs if m["i"] in unread_ids and not m["from_me"]]
    rel = lambda m: m["mention"] or m["broad"] or m["action"] or m["llm_action"]
    recent = lambda m: (ref - m["deadline"]).days <= 14  # ancient deadlines are stale, not "missed"
    missed = sorted([m for m in U if m["deadline"] and m["overdue"] and recent(m) and not m["handled"] and rel(m)], key=lambda m: -m["score"])
    ms = {m["i"] for m in missed}
    needs = sorted([m for m in U if m["i"] not in ms and m["score"] >= .25 and (m["mention"] or m["question"] or m["llm_action"] or (m["action"] and m["broad"]))],
                   key=lambda m: (m["handled"], -m["score"]))
    ahead = sorted([m for m in U if m["deadline"] and not m["overdue"]], key=lambda m: m["deadline"])
    decisions = [m for m in U if m["decision"] or m["llm_decision"]]
    sender_counts = {}
    for i in unread_ids:
        sender = msgs[i].get("who", "Unknown")
        sender_counts[sender] = sender_counts.get(sender, 0) + 1
    rep = {"msgs": msgs, "ref": ref, "media": media, "seen": seen, "sender_counts": sorted(sender_counts.items(), key=lambda item: (-item[1], item[0].lower())), "n_unread": len(unread_ids), "people": len({msgs[i]["who"] for i in unread_ids}),
           "missed": missed, "needs": needs, "ahead": ahead, "decisions": decisions, "ai": ai,
           "skip": len(unread_ids) - len({m["i"] for m in missed + needs + ahead + decisions})}
    live = [m for m in needs if not m["handled"]]
    rep["tldr"] = (f"{rep['n_unread']} messages from {rep['people']} people. The rule-based scan found "
                   f"{len(missed)} possibly missed messages, {len(live)} messages needing a reply or action, "
                   f"{len(ahead)} upcoming deadlines, and {len(decisions)} decisions.")
    if llm and ai["summaries"]:
        hint = "\n".join([f"MISSED: {m['text']}" for m in missed] + [f"FOR ME: {m['text']}" for m in needs[:6]] + [f"DECISION: {m['text']}" for m in decisions[:5]])
        evidence_lines, evidence_size = [], 0
        for m in [item for item in msgs if item["i"] in unread_ids][-max(1, int(ai_limit)):]:
            line = f"[{m['i']}] {m['t']:%d %b %H:%M} | {m['who']}: {m['text']}"
            if evidence_size + len(line) > 14000:
                evidence_lines.append("[Remaining messages omitted from evidence due to context limit]")
                break
            evidence_lines.append(line)
            evidence_size += len(line)
        try:
            if progress:
                progress(.94, "Writing the full-period conclusion")
            ai["tldr"] = llm_tldr(
                llm, names, ai["summaries"], hint[:2000], "\n".join(evidence_lines),
                relationship, assess_relationship_dynamic,
            )
            if progress:
                progress(1.0, "Done")
        except Exception as e:
            ai["errors"].append(str(e))
    return rep


def to_markdown(rep):
    f = lambda d: d.strftime("%a %d %b, %I:%M %p")
    out = ["# What did I miss?", "", rep["tldr"], ""]
    if rep["ai"]["tldr"]: out += ["## AI summary (local)", rep["ai"]["tldr"], ""]
    for title, key in (("Possibly missed", "missed"), ("Needs you", "needs"), ("Coming up", "ahead"), ("Decisions", "decisions")):
        out.append(f"## {title}")
        for m in rep[key]:
            due = f" (due {f(m['deadline'])})" if m["deadline"] else ""
            out.append(f"- **{m['who']}** {f(m['t'])}: {m['text'].strip()}{due}")
        out.append("")
    out += ["## Conversation transcript", ""]
    cutoff = rep.get("seen")
    visible = [m for m in rep.get("msgs", []) if cutoff is None or m["t"] > cutoff]
    for m in visible:
        out.append(f"- **{m['who']}** ({f(m['t'])}): {m['text'].strip()}")
    out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="Catch up on a chat export, locally.")
    ap.add_argument("file", help="WhatsApp export .zip (with images) or .txt"); ap.add_argument("--me", required=True, help="comma-separated names")
    ap.add_argument("--model", default=None, help="Ollama model, e.g. gemma3:4b (omit = rules only)")
    a = ap.parse_args()
    msgs, dmy, media, prev = load_files([(a.file, open(a.file, "rb").read())])
    if not msgs: sys.exit("No messages found. File starts with:\n" + prev)
    rep = analyze(msgs, dmy, [n.strip() for n in a.me.split(",")], llm=LocalLLM(a.model) if a.model else None, media=media)
    print(to_markdown(rep))
    if rep["ai"]["errors"]: print("AI notes:", rep["ai"]["errors"], file=sys.stderr)
