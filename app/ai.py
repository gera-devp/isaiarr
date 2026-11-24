"""Talk to an AI model: Gemini, Groq, OpenRouter, OpenAI, Anthropic, Ollama or any OpenAI-compatible server.

Plain HTTP (no vendor SDKs). Every call asks for JSON and returns (data, usage). Usage is logged
in ai_usage so the app can show what playlists cost and stop at a monthly limit.

Reliability: a busy service (HTTP 500/502/503/504) is asked again twice with a pause; a rate
limit (429) waits as long as the service says (or 20, 40, then 60 s) and asks again, up to three times. A used-up daily quota isn't
retried. When that's not enough, a second service (AI_FALLBACK) gets the same request.
"""
import json
import re
import threading
import time

import requests

from . import db
from .config import Config

PROVIDERS = {
    # key: (label, default model, songs per request, $ per 1M input, $ per 1M output)
    "gemini": ("Google Gemini", "gemini-3.8-flash", 1500, 0.30, 2.50),   # newest Flash is found automatically
    "groq": ("Groq", "openai/gpt-oss-120b", 150, 0.15, 0.60),   # Llama left Groq's free tier in Aug 2026
    "openrouter": ("OpenRouter", "openai/gpt-oss-120b:free", 500, 0.0, 0.0),   # ":free" models cost nothing
    "mistral": ("Mistral", "mistral-small-latest", 800, 0.10, 0.30),   # free "Experiment" plan: phone number, no card
    "openai": ("OpenAI", "gpt-4.1-mini", 800, 0.40, 1.60),
    "anthropic": ("Anthropic Claude", "claude-haiku-4-5", 800, 1.00, 5.00),
    "ollama": ("Ollama (your own server)", "qwen2.5:7b", 200, 0.0, 0.0),
    "custom": ("OpenAI-compatible server", "", 300, 0.0, 0.0),
}
TOKENS_PER_SONG = 28          # one "id|title|singers|director|film|year|mood" line, roughly
_rate_lock = threading.Lock()
_last_call = [0.0]


class AIError(Exception):
    pass


class ModelNotFound(AIError):
    pass


class AITimeout(AIError):
    pass


class AIQuota(AIError):
    """A daily (or monthly) quota is used up: waiting a minute won't help."""


class AIFormat(AIError):
    """The answer came back, but not as the JSON we asked for (often cut off)."""


QUOTA_PAUSE = 3600            # after a used-up daily quota, skip that service for an hour when there's a second one
_quota_until = {}


FREE_TIERS = ("gemini", "groq", "openrouter", "mistral")


def provider():
    return Config.AI_PROVIDER if Config.AI_PROVIDER in PROVIDERS else "off"


def enabled():
    p = provider()
    return p != "off" and ready(p)


def _key(p):
    return {"gemini": Config.GEMINI_API_KEY, "groq": Config.GROQ_API_KEY, "openai": Config.OPENAI_API_KEY,
            "anthropic": Config.ANTHROPIC_API_KEY, "custom": Config.CUSTOM_AI_KEY,
            "openrouter": Config.OPENROUTER_API_KEY, "mistral": Config.MISTRAL_API_KEY}.get(p, "")


def ready(p):
    """This service has what it needs (key or address)."""
    if p not in PROVIDERS:
        return False
    if p == "ollama":
        return bool(Config.OLLAMA_URL)
    if p == "custom":
        return bool(Config.CUSTOM_AI_URL)
    return bool(_key(p))


def fallback():
    """The second service, when one is set, ready and different from the first."""
    fb = Config.AI_FALLBACK
    return fb if fb not in ("off", "", provider()) and ready(fb) else ""


def model_for(p, chosen=""):
    """The model you set, else the one found automatically, else the built-in default."""
    if chosen:
        return chosen
    return (db.state_get(f"ai_auto_model_{p}") or "") or PROVIDERS.get(p, ("", ""))[1]


def model():
    return model_for(provider(), Config.AI_MODEL)


def fallback_model():
    return model_for(fallback(), Config.AI_FALLBACK_MODEL)


_SKIP = ("lite", "tts", "image", "live", "audio", "embedding", "preview", "exp", "thinking", "learnlm", "nano",
         "banana", "veo", "imagen", "robotics", "computer", "native")


def _version(name):
    import re as _re
    m = _re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def discover_gemini():
    """Ask Google which models this key can use and pick the newest plain Flash model.
    Google retires and limits older models over time; this keeps Isaiarr working without edits."""
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 1000},
                         headers={"x-goog-api-key": Config.GEMINI_API_KEY}, timeout=30)
    except requests.RequestException as e:
        raise AIError(f"Couldn't reach Gemini: {e}")
    if r.status_code in (401, 403):
        raise AIError("The AI service rejected the API key (Settings → AI)")
    if r.status_code >= 400:
        raise AIError(f"Couldn't list Gemini models ({r.status_code})")
    names = [m.get("name", "").split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    flash = [n for n in names if "flash" in n and not any(x in n for x in _SKIP)]
    pool = flash or [n for n in names if "flash" in n and "lite" in n and not any(x in n for x in _SKIP if x != "lite")]
    if not pool:
        raise AIError("No usable Gemini Flash model for this key; set a model name in Settings → AI")
    best = max(pool, key=lambda n: (_version(n), "latest" not in n, -len(n)))
    db.state_set("ai_auto_model_gemini", best)
    db.log(f"Gemini: using model {best}")
    return best


def label():
    p = provider()
    if p not in PROVIDERS:
        return "Off"
    fb = fallback()
    return f"{PROVIDERS[p][0]} · {model()}" + (f" (then {PROVIDERS[fb][0]})" if fb else "")


def max_songs():
    """Songs per request. With a second service, the smaller of the two, so the same request fits
    either one."""
    if Config.AI_MAX_SONGS:
        return Config.AI_MAX_SONGS
    n = PROVIDERS.get(provider(), (0, 0, 200))[2]
    fb = fallback()
    return min(n, PROVIDERS[fb][2]) if fb else n


def prices(p=None, m=None):
    p = p or provider()
    info = PROVIDERS.get(p, ("", "", 0, 0.0, 0.0))
    if p == "openrouter" and (m or model_for(p, Config.AI_MODEL if p == provider() else Config.AI_FALLBACK_MODEL)).endswith(":free"):
        return 0.0, 0.0
    if Config.AI_FREE_TIER and p in FREE_TIERS:
        return 0.0, 0.0
    if p != provider():
        return info[3], info[4]
    pin = Config.AI_PRICE_IN if Config.AI_PRICE_IN >= 0 else info[3]
    pout = Config.AI_PRICE_OUT if Config.AI_PRICE_OUT >= 0 else info[4]
    return pin, pout


def estimate(n_songs, out_tokens=1500):
    """(input tokens, cost in $) for a request that lists n_songs."""
    tin = 900 + n_songs * TOKENS_PER_SONG
    pin, pout = prices()
    return tin, round((tin * pin + out_tokens * pout) / 1e6, 4)


def month_spend():
    start = time.mktime(time.localtime()[:2] + (1, 0, 0, 0, 0, 0, -1))
    r = db.q("SELECT COALESCE(SUM(cost),0) c, COALESCE(SUM(in_tokens),0) i, COALESCE(SUM(out_tokens),0) o, "
             "COUNT(*) n FROM ai_usage WHERE ts >= ?", (start,), one=True)
    return {"cost": round(r["c"], 4), "in": r["i"], "out": r["o"], "calls": r["n"]}


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except ValueError:
            pass
    raise AIFormat("The AI didn't answer in the expected format (the answer may have been cut off)")


def _pace():
    """Stay under the provider's requests-per-minute limit (free tiers are strict)."""
    gap = 60.0 / max(1, Config.AI_RPM)
    with _rate_lock:
        wait = _last_call[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _retry_delay(r):
    """Seconds the service asks us to wait: Retry-After, or Gemini's RetryInfo ("43s")."""
    try:
        wait = float((getattr(r, "headers", None) or {}).get("retry-after") or 0)
    except (TypeError, ValueError):
        wait = 0
    if not wait:
        try:
            for d in ((r.json() or {}).get("error") or {}).get("details") or []:
                m = re.match(r"^([\d.]+)s$", str(d.get("retryDelay") or ""))
                if m:
                    wait = float(m.group(1))
        except (ValueError, AttributeError):
            pass
    return wait


def _error_text(r):
    try:
        msg = r.json()
        msg = (msg.get("error") or {}).get("message") if isinstance(msg.get("error"), dict) else msg.get("error") or msg
    except (ValueError, AttributeError):
        msg = (getattr(r, "text", "") or "")[:200]
    return str(msg)[:300]


def _daily_quota(r):
    """A 429 that's about a per-day (or per-month) quota, not requests per minute."""
    t = (getattr(r, "text", "") or "").lower()
    try:
        t += json.dumps(r.json()).lower()
    except (ValueError, AttributeError, TypeError):
        pass
    return any(w in t for w in ("perday", "per day", "per_day", "per-day", "daily", "permonth", "per month",
                                "per-month", "credits"))


BUSY = (500, 502, 503, 504, 529)
BUSY_WAITS = (4, 12)
RATE_WAITS = (20, 40, 60)     # a per-minute limit without "wait N seconds": pause this long, then ask again


def _post(url, body, headers, timeout, retry=True, model_name=""):
    """POST and return the JSON answer. Busy (5xx) answers are retried twice with a pause; a
    per-minute rate limit waits up to three times: as long as the service asks, else 20, 40, 60 s."""
    waits_429 = 0 if retry else len(RATE_WAITS)
    attempt = 0
    while True:
        try:
            # connect quickly; allow a long wait for the answer (thinking models can be slow)
            r = requests.post(url, json=body, headers=headers, timeout=(15, timeout))
        except requests.Timeout:
            raise AITimeout(f"The AI service didn't answer within {int(timeout)} seconds")
        except requests.ConnectionError as e:
            raise AIError(f"Couldn't reach the AI service (check the NAS's internet/DNS): {e}")
        except requests.RequestException as e:
            raise AIError(f"Couldn't reach the AI service: {e}")
        if r.status_code in BUSY and retry and attempt < len(BUSY_WAITS):
            time.sleep(BUSY_WAITS[attempt])
            attempt += 1
            continue
        if r.status_code == 429:
            if _daily_quota(r):
                raise AIQuota(f"The AI service's daily quota is used up: {_error_text(r)}")
            asked = _retry_delay(r)
            wait = asked if asked else (RATE_WAITS[waits_429] if waits_429 < len(RATE_WAITS) else 0)
            if waits_429 < len(RATE_WAITS) and 0 < wait <= 90:
                waits_429 += 1
                db.log(f"The AI service is over its per-minute limit; waiting {int(wait)} s and asking again "
                       f"({waits_429} of {len(RATE_WAITS)})", "warn")
                time.sleep(wait)
                with _rate_lock:          # everything else waits too, instead of hitting the limit again
                    _last_call[0] = max(_last_call[0], time.time())
                continue
            raise AIError(f"The AI service is still over its rate limit after waiting {len(RATE_WAITS)} times "
                          f"(free plans allow only a few requests a minute; lower “Requests per minute at most” "
                          f"in Settings → AI, or try again later): {_error_text(r)}")
        break
    if r.status_code in (401, 403):
        raise AIError("The AI service rejected the API key (Settings → AI)")
    if r.status_code == 402:
        raise AIQuota(f"The AI service says the account is out of credit: {_error_text(r)}")
    if r.status_code == 404:
        raise ModelNotFound(f"Model or address not found: {model_name or model()} (Settings → AI)")
    if r.status_code in BUSY:
        raise AIError(f"The AI service is busy ({r.status_code}) and didn't answer after 3 tries: {_error_text(r)}")
    if r.status_code >= 400:
        raise AIError(f"AI request failed ({r.status_code}): {_error_text(r)}")
    try:
        return r.json()
    except ValueError:
        raise AIError("The AI service sent back something that isn't JSON")


def _openai_style(base, key, system, user, temperature, timeout, json_mode=True, mdl="", extra_headers=None):
    mdl = mdl or model()
    if not json_mode:
        user += "\n\nAnswer with JSON only."
    body = {"model": mdl, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if "gpt-oss" in mdl and "groq.com" in base:
        body["reasoning_effort"] = "low"          # reasoning model: keep answers quick and within free-tier tokens
    if "gpt-oss" in mdl and "openrouter.ai" in base:
        body["reasoning"] = {"effort": "low"}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    headers.update(extra_headers or {})
    d = _post(base.rstrip("/") + "/chat/completions", body, headers, timeout, model_name=mdl)
    if isinstance(d, dict) and d.get("error") and not d.get("choices"):
        # OpenRouter passes on the model host's error inside a 200 answer
        e = d["error"]
        msg = e.get("message") if isinstance(e, dict) else str(e)
        code = e.get("code") if isinstance(e, dict) else None
        if code == 429 or "rate" in str(msg).lower():
            raise AIError(f"The AI service says too many requests: {msg}")
        raise AIError(f"The AI service failed: {msg}")
    try:
        text = d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("Unexpected answer from the AI service")
    if not text:
        raise AIFormat("The AI service sent back an empty answer")
    u = d.get("usage") or {}
    return text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def complete(system, user, purpose="playlist", temperature=0.7, timeout=None):
    """Ask the configured model; if it fails, the second service (Settings → AI). Returns
    (parsed JSON, usage dict)."""
    if not enabled():
        raise AIError("AI is not set up (Settings → AI)")
    chain = [(provider(), model())] + ([(fallback(), fallback_model())] if fallback() else [])
    if len(chain) > 1 and _quota_until.get(chain[0][0], 0) > time.time():
        chain.reverse()                     # the first service's daily quota ran out a moment ago: start with the second
    last = None
    for i, (p, mdl) in enumerate(chain):
        if Config.AI_MONTHLY_LIMIT > 0 and prices(p, mdl) != (0.0, 0.0):
            if month_spend()["cost"] >= Config.AI_MONTHLY_LIMIT:
                last = AIError(f"Monthly AI limit of ${Config.AI_MONTHLY_LIMIT:g} reached (Settings → AI)")
                continue
        tries = 1 if i + 1 < len(chain) else 2          # the last service gets a second go at a cut-off answer
        for t in range(tries):
            try:
                data, usage = _complete_one(p, mdl, system, user, purpose, temperature, timeout)
                if p != provider():
                    usage["fallback"] = True
                _quota_until.pop(p, None)
                return data, usage
            except AIFormat as e:
                last = e
                if t + 1 < tries:
                    db.log(f"{PROVIDERS[p][0]}: {e}; asking again", "warn")
                    temperature = min(temperature, 0.3)
            except AIError as e:
                last = e
                if isinstance(e, AIQuota):
                    _quota_until[p] = time.time() + QUOTA_PAUSE
                break
        if i + 1 < len(chain):
            db.log(f"{PROVIDERS[p][0]} failed ({last}); asking {PROVIDERS[chain[i + 1][0]][0]} instead", "warn")
    if len(chain) > 1:
        raise AIError(f"Both AI services failed. {PROVIDERS[chain[-1][0]][0]}: {last}")
    raise last


def _complete_one(p, mdl, system, user, purpose, temperature, timeout):
    _pace()
    timeout = timeout or Config.AI_TIMEOUT
    if p == "gemini":
        gen = {"temperature": temperature, "responseMimeType": "application/json"}
        level = Config.AI_THINKING
        if level != "default" and not db.state_get("ai_gemini_no_thinking_level"):
            # Gemini 3 models "think" at medium by default; picking songs doesn't need much of it
            gen["thinkingConfig"] = {"thinkingLevel": level}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
        hdr = {"x-goog-api-key": Config.GEMINI_API_KEY}

        chosen = Config.AI_MODEL if p == provider() else Config.AI_FALLBACK_MODEL
        cur = {"m": mdl}

        def call():
            try:
                return _post(url.format(cur["m"]), body, hdr, timeout, model_name=cur["m"])
            except ModelNotFound:
                if chosen:                    # you chose this model: say so instead of switching behind your back
                    raise ModelNotFound(f"Gemini doesn't offer “{chosen}” to this key. Clear the Model field "
                                        "in Settings → AI to pick the newest Flash model automatically.")
                cur["m"] = discover_gemini()
                return _post(url.format(cur["m"]), body, hdr, timeout, model_name=cur["m"])
        try:
            d = call()
        except AITimeout:
            if "thinkingConfig" in gen and gen["thinkingConfig"].get("thinkingLevel") != "low":
                gen["thinkingConfig"] = {"thinkingLevel": "low"}
            db.log("Gemini was slow to answer; trying once more", "warn")
            try:
                d = call()
            except AITimeout:
                raise AITimeout(f"Gemini didn't answer within {int(timeout)} seconds, twice. It may be busy: "
                                "try again later, or set Thinking to Low (Settings → AI).")
        except AIError as e:
            if "thinkingConfig" in gen and "thinking" in str(e).lower():
                # this model doesn't take a thinking level: remember and carry on without it
                db.state_set("ai_gemini_no_thinking_level", True)
                gen.pop("thinkingConfig")
                d = call()
            else:
                raise
        try:
            parts = d["candidates"][0]["content"]["parts"]
            text = "".join(x.get("text", "") for x in parts if not x.get("thought"))
        except (KeyError, IndexError, TypeError):
            reason = ((d.get("promptFeedback") or {}).get("blockReason")
                      or ((d.get("candidates") or [{}])[0].get("finishReason")))
            raise AIError(f"Gemini returned no answer ({reason or 'unknown reason'})")
        u = d.get("usageMetadata") or {}
        tin, tout = u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        mdl = cur["m"]
    elif p == "groq":
        text, tin, tout = _openai_style("https://api.groq.com/openai/v1", Config.GROQ_API_KEY, system, user,
                                        temperature, timeout, mdl=mdl)
    elif p == "openrouter":
        text, tin, tout = _openai_style("https://openrouter.ai/api/v1", Config.OPENROUTER_API_KEY, system, user,
                                        temperature, timeout, json_mode=False, mdl=mdl,
                                        extra_headers={"X-Title": "Isaiarr", "HTTP-Referer": "https://github.com/gera-devp/isaiarr"})
    elif p == "mistral":
        text, tin, tout = _openai_style("https://api.mistral.ai/v1", Config.MISTRAL_API_KEY, system, user,
                                        temperature, timeout, mdl=mdl)
    elif p == "openai":
        text, tin, tout = _openai_style("https://api.openai.com/v1", Config.OPENAI_API_KEY, system, user,
                                        temperature, timeout, mdl=mdl)
    elif p == "custom":
        text, tin, tout = _openai_style(Config.CUSTOM_AI_URL, Config.CUSTOM_AI_KEY, system, user, temperature,
                                        timeout, json_mode=False, mdl=mdl)
    elif p == "anthropic":
        body = {"model": mdl, "max_tokens": 4096, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": user + "\n\nAnswer with JSON only."}]}
        d = _post("https://api.anthropic.com/v1/messages", body,
                  {"x-api-key": Config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}, timeout, model_name=mdl)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage") or {}
        tin, tout = u.get("input_tokens", 0), u.get("output_tokens", 0)
    elif p == "ollama":
        body = {"model": mdl, "stream": False, "format": "json",
                "options": {"temperature": temperature, "num_ctx": Config.OLLAMA_CONTEXT},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        d = _post(Config.OLLAMA_URL.rstrip("/") + "/api/chat", body, {}, max(timeout, 900), model_name=mdl)
        text = (d.get("message") or {}).get("content", "")
        tin, tout = d.get("prompt_eval_count", 0), d.get("eval_count", 0)
    else:
        raise AIError("AI is off")
    pin, pout = prices(p, mdl)
    cost = round((tin * pin + tout * pout) / 1e6, 5)
    db.x("INSERT INTO ai_usage(ts,provider,model,purpose,in_tokens,out_tokens,cost) VALUES(?,?,?,?,?,?,?)",
         (time.time(), p, mdl, purpose, tin, tout, cost))
    return parse_json(text), {"provider": p, "model": mdl, "in": tin, "out": tout, "cost": cost}


def test():
    """Tiny request for the Settings page."""
    data, u = complete("You are a test. Reply with JSON.", 'Reply exactly {"ok": true, "song": "<any famous Tamil film song>"}',
                       purpose="test", temperature=0)
    return data, u
