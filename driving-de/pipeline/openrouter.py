"""Tiny OpenRouter client used by the sign-quiz pipeline (Sonnet = writer, Haiku = reviewer)."""
import json, os, re, time, urllib.request

SONNET = "anthropic/claude-sonnet-5.5"
HAIKU = "anthropic/claude-haiku-5.5"

def chat(model, system, user, max_tokens=8000, temperature=0.3):
    body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"], "Content-Type": "application/json",
                 "X-Title": "German Sign Quiz"})
    for attempt in range(4):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=300))
            return d["choices"][0]["message"]["content"]
        except Exception as e:
            print("  openrouter retry", attempt, e); time.sleep(5 * (attempt + 1))
    raise RuntimeError("OpenRouter failed")

def chat_json(model, system, user, **kw):
    txt = chat(model, system, user, **kw)
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    txt = m.group(1) if m else txt
    s = min([i for i in (txt.find("["), txt.find("{")) if i >= 0])
    return json.loads(txt[s:])
