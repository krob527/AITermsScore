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
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)

@app.after_request
def set_language_header(response):
    response.headers["Content-Language"] = "en"
    return response

# ─────────────────────────────────────────────────────────────────────────────
# In-process job store  { job_id: { status, result, queue } }
# ─────────────────────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/score", methods=["POST"])
def score():
    data = request.get_json(silent=True) or {}
    product_name = data.get("product_name", "").strip()
    vendor       = data.get("vendor", "").strip()

    if not product_name:
        return jsonify({"error": "product_name is required"}), 400

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()

    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "result": None, "queue": q}

    t = threading.Thread(target=_run_score, args=(job_id, product_name, vendor), daemon=True)
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


def _run_score(job_id: str, product_name: str, vendor: str):
    q = _jobs[job_id]["queue"]
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
        )

        _emit(q, "status", message="Writing output files…")
        paths = write_outputs(result, cfg.output_dir)

        structured = result.structured if isinstance(result.structured, dict) else {}
        # Guarantee overall_score is a plain float at the top level
        overall_val = structured.get("overall")
        # Coerce string numerics (agent sometimes returns "3.5" instead of 3.5)
        if isinstance(overall_val, str):
            try:
                overall_val = float(overall_val)
            except (ValueError, TypeError):
                overall_val = None
        if not isinstance(overall_val, (int, float)):
            dim_scores = [
                float(v["score"]) for v in structured.values()
                if isinstance(v, dict) and (
                    isinstance(v.get("score"), (int, float))
                    or (isinstance(v.get("score"), str) and str(v["score"]).replace(".", "", 1).isdigit())
                )
            ]
            overall_val = round(sum(dim_scores) / len(dim_scores), 2) if dim_scores else None

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

        _emit(q, "done", result=payload)

    except Exception as exc:  # pylint: disable=broad-exception-caught
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
        _emit(q, "error", message=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
