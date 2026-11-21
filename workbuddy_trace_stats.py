#!/usr/bin/env python3
"""Scan WorkBuddy trace jsonl files and produce a single-file HTML stats report."""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta


def parse_ts(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt
        return dt.astimezone()
    except (ValueError, TypeError):
        return None


def day_key(dt):
    return dt.date().isoformat() if dt else ""


def esc(value):
    return html.escape(str(value), quote=True)


def short_model(name):
    if not name:
        return "(unknown)"
    return name.split("/")[-1] if "/" in name else name


def new_totals():
    return {
        "sessions": 0, "calls": 0,
        "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0, "total": 0,
        "duration": 0, "gen_duration": 0,
    }


def scan(traces_dir, since_days=None, limit=0):
    files = glob.glob(os.path.join(traces_dir, "*", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if limit:
        files = files[:limit]

    cutoff = None
    if since_days:
        cutoff = datetime.now().astimezone() - timedelta(days=since_days)

    totals = new_totals()
    by_model = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0,
                                    "cached": 0, "reasoning": 0, "total": 0, "duration": 0})
    by_day = defaultdict(lambda: {"sessions": 0, "calls": 0, "prompt": 0,
                                  "completion": 0, "cached": 0, "total": 0})
    by_span = defaultdict(lambda: {"count": 0, "duration": 0})
    sessions = []
    malformed = 0

    for index, path in enumerate(files, 1):
        if index % 1000 == 0:
            print(f"  scanned {index}/{len(files)} ...", file=sys.stderr)

        events = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"span_end"' not in line and '"trace_start"' not in line and '"trace_end"' not in line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        malformed += 1
        except OSError:
            continue

        trace_start = None
        trace_end = None
        spans = []
        for ev in events:
            kind = ev.get("type")
            if kind == "trace_start":
                trace_start = ev.get("trace") or {}
            elif kind == "trace_end":
                trace_end = ev.get("trace") or {}
            elif kind == "span_end":
                spans.append(ev.get("span") or {})

        started = parse_ts((trace_start or {}).get("startedAt"))
        if not started:
            started = parse_ts((trace_end or {}).get("endedAt"))
        if cutoff and started and started < cutoff:
            continue
        dk = day_key(started)
        if not dk:
            continue

        trace_duration = (trace_end or {}).get("duration")
        if trace_duration is None:
            ends = [s.get("endedAt") for s in spans if s.get("endedAt")]
            if ends:
                last = parse_ts(max(ends))
                if last and started:
                    trace_duration = int((last - started).total_seconds() * 1000)
        trace_duration = trace_duration or 0

        totals["sessions"] += 1
        by_day[dk]["sessions"] += 1
        totals["duration"] += trace_duration

        for span in spans:
            name = span.get("name") or "(unnamed)"
            span_type = span.get("type") or "unknown"
            duration = span.get("duration") or 0
            by_span[(name, span_type)]["count"] += 1
            by_span[(name, span_type)]["duration"] += duration

            if span_type != "generation":
                continue

            model = ""
            usage = None
            raw = span.get("toolOutput")
            if raw:
                try:
                    arr = json.loads(raw)
                    if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                        model = arr[0].get("model") or ""
                        usage = arr[0].get("usage")
                except (json.JSONDecodeError, TypeError):
                    pass
            if not isinstance(usage, dict):
                continue

            prompt = usage.get("prompt_tokens") or 0
            completion = usage.get("completion_tokens") or 0
            total = usage.get("total_tokens") or (prompt + completion)
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0

            m = short_model(model)
            totals["calls"] += 1
            totals["gen_duration"] += duration
            for key, value in (("prompt", prompt), ("completion", completion),
                               ("cached", cached), ("reasoning", reasoning), ("total", total)):
                totals[key] += value
            by_model[m]["calls"] += 1
            for key, value in (("prompt", prompt), ("completion", completion),
                               ("cached", cached), ("reasoning", reasoning), ("total", total)):
                by_model[m][key] += value
            by_model[m]["duration"] += duration
            by_day[dk]["calls"] += 1
            for key, value in (("prompt", prompt), ("completion", completion),
                               ("cached", cached), ("total", total)):
                by_day[dk][key] += value

        sessions.append({
            "day": dk,
            "at": started.strftime("%Y-%m-%d %H:%M") if started else "",
            "duration": trace_duration,
            "spans": len(spans),
        })

    sessions.sort(key=lambda s: (s["day"], s["at"]), reverse=True)
    result = {
        "totals": totals,
        "by_model": dict(by_model),
        "by_day": dict(by_day),
        "by_span": [{"name": k[0], "type": k[1], **v}
                    for k, v in by_span.items()],
        "sessions": sessions[:300],
        "file_count": len(files),
        "malformed": malformed,
    }
    return result


def render(result, template_path, out_path):
    payload = dict(result)
    payload["generated_at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    payload_json = payload_json.replace("<", "\\u003c").replace(">", "\\u003e")

    with open(template_path, encoding="utf-8") as fh:
        doc = fh.read()
    doc = doc.replace("__DATA__", payload_json)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)


def main():
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(description="WorkBuddy trace token/duration stats -> HTML")
    parser.add_argument("--traces", default=os.path.join(home, ".workbuddy", "traces"),
                        help="traces directory (default: ~/.workbuddy/traces)")
    here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--out", default=os.path.join(here, "workbuddy_stats.html"),
                        help="output HTML path (default: alongside this script)")
    parser.add_argument("--template", default=os.path.join(here, "report_template.html"),
                        help="HTML template path (default: report_template.html alongside this script)")
    parser.add_argument("--days", type=int, default=0, help="only last N days (0 = all)")
    parser.add_argument("--limit", type=int, default=0, help="only newest N files (debug)")
    args = parser.parse_args()

    result = scan(args.traces, args.days, args.limit)
    render(result, args.template, args.out)
    t = result["totals"]
    print(f"sessions={t['sessions']} calls={t['calls']} tokens={t['total']} "
          f"cached={t['cached']} -> {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
