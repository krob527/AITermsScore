"""
app.py – Flask web interface for AITermsScore.

Usage:
    python app.py
    # Then open http://localhost:5000
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)

@app.after_request
def set_language_header(response):
    response.headers["Content-Language"] = "en"
    return response

# ─────────────────────────────────────────────────────────────────────────────
# In-process job store  { job_id: { status, result, queue, created_at } }
# Jobs are evicted after JOB_TTL_SECONDS to prevent unbounded memory growth.
# ─────────────────────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
JOB_TTL_SECONDS = 600  # 10 minutes

# ─────────────────────────────────────────────────────────────────────────────
# In-flight deduplication  { normalised_key: job_id }
# Returns the existing job_id when the same product is already being scored.
# ─────────────────────────────────────────────────────────────────────────────
_inflight: dict[str, str] = {}

# ─────────────────────────────────────────────────────────────────────────────
# Per-IP rate limiting  { ip: [timestamp, ...] }
# Max RATE_LIMIT_MAX_REQUESTS per RATE_LIMIT_WINDOW_SECONDS rolling window.
# ─────────────────────────────────────────────────────────────────────────────
_rate_data: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 5

# Input limits
MAX_PRODUCT_NAME_LEN = 200


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _evict_expired_jobs() -> None:
    """Remove jobs older than JOB_TTL_SECONDS. Must be called under _jobs_lock."""
    cutoff = time.monotonic() - JOB_TTL_SECONDS
    expired = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
    for jid in expired:
        normalised = _jobs[jid].get("normalised_key")
        if normalised and _inflight.get(normalised) == jid:
            _inflight.pop(normalised, None)
        del _jobs[jid]


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded the rate limit."""
    now = time.monotonic()
    with _rate_lock:
        timestamps = _rate_data.get(ip, [])
        timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            _rate_data[ip] = timestamps
            return True
        timestamps.append(now)
        _rate_data[ip] = timestamps
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/score", methods=["POST"])
def score():
    # Rate limiting
    client_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
        .split(",")[0]
        .strip()
    )
    if _is_rate_limited(client_ip):
        return jsonify({"error": "Too many requests. Please wait before scoring again."}), 429

    data = request.get_json(silent=True) or {}
    product_name = data.get("product_name", "").strip()
    vendor       = data.get("vendor", "").strip()

    if not product_name:
        return jsonify({"error": "product_name is required"}), 400

    if len(product_name) > MAX_PRODUCT_NAME_LEN:
        return jsonify({"error": f"product_name must be {MAX_PRODUCT_NAME_LEN} characters or fewer"}), 400

    # Deduplication – reuse an in-flight job for the same product
    normalised_key = product_name.lower()
    with _jobs_lock:
        _evict_expired_jobs()
        if normalised_key in _inflight:
            return jsonify({"job_id": _inflight[normalised_key], "reused": True})

        job_id = str(uuid.uuid4())
        q: queue.Queue = queue.Queue()
        _jobs[job_id] = {
            "status": "pending",
            "result": None,
            "queue": q,
            "created_at": time.monotonic(),
            "normalised_key": normalised_key,
        }
        _inflight[normalised_key] = job_id

    t = threading.Thread(target=_run_score, args=(job_id, product_name, vendor, q), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    def generate():
        q: queue.Queue = job["queue"]
        while True:
            try:
                event = q.get(timeout=90)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield "data: {\"type\":\"heartbeat\"}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Background scoring worker
# ─────────────────────────────────────────────────────────────────────────────

def _emit(q: queue.Queue, type_: str, **kwargs):
    q.put({"type": type_, **kwargs})


def _run_score(job_id: str, product_name: str, vendor: str, q: queue.Queue):
    """Background worker – q is passed in directly (captured under _jobs_lock before thread spawn)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env", override=True)

        from config import load_config
        from agent.setup import get_or_create_agent
        from agent.runner import run_scoring
        from output_writer import write_outputs

        _emit(q, "status", message="Loading configuration…")
        cfg = load_config()

        _emit(q, "status", message="Connecting to AI Foundry and provisioning agent…")
        client, agent = get_or_create_agent(cfg)
        _emit(q, "status", message=f"Agent ready: {agent.id[:30]}…")

        _emit(q, "status", message=f"Searching and scoring {product_name} – this may take 1–3 minutes…")
        result = run_scoring(
            client=client,
            agent=agent,
            product_name=product_name,
            vendor=vendor,
            on_status=lambda msg: _emit(q, "status", message=msg),
        )

        _emit(q, "status", message="Writing output files…")
        write_outputs(result, cfg.output_dir)

        # overall_score is already normalised by parse_scorecard() in runner.py
        structured = result.structured if isinstance(result.structured, dict) else {}
        overall_val = structured.get("overall")

        payload = {
            "product_name":  result.product_name,
            "vendor":        result.vendor,
            "run_id":        result.run_id,
            "structured":    structured,
            "overall_score": overall_val,
            "raw_markdown":  result.raw_markdown,
        }

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = payload
            _inflight.pop(_jobs[job_id].get("normalised_key", ""), None)

        _emit(q, "done", result=payload)

    except Exception as exc:  # pylint: disable=broad-exception-caught
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = "error"
                _inflight.pop(_jobs[job_id].get("normalised_key", ""), None)
        _emit(q, "error", message=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
