# What Did I Miss? (local-first chat triage)

## Setup (Mac)
1. Install Ollama from https://ollama.com, then in Terminal: `ollama pull gemma3:4b`
2. `cd` into this folder, then: `pip3 install -r requirements.txt`
3. `streamlit run app.py`   (do NOT use the VS Code play button)

## Use
Upload the WhatsApp export .zip (Export chat > Attach media), or the .txt plus its images.
The app lists the people in the chat and guesses which one you are. Change it if wrong.

## How it works
parse -> rule signals (mentions, deadlines, urgency, actions, decisions)
      -> local vision model reads images (OCR + description) into the text
      -> local LLM over 40-message chunks (summary, decisions, action items)
      -> merged priority score -> report + final AI catch-up

Privacy: the LLM host is locked to localhost. No other network calls exist.
Without Ollama the rule-based analysis still runs. Image reading needs a vision model (gemma3:4b or larger).
CLI: `python3 engine.py export.zip --me "Your Name" --model gemma3:4b`
