# Development rules

- Python 3.11+; keep HTTP forwarding separate from routing and protocol inspection.
- Run `python -m pytest` and `python -m compileall -q src tests` before proposing changes.
- Do not commit credentials, `.env`, SQLite state, request captures, or real prompts.
- Preserve Responses tool/vision/compaction payloads and SSE bytes. Never replay a generation automatically.
- Model and effort changes must respect explicit selections, configured caps, and linked/opaque-state affinity.
- Unknown opaque state must fail closed. Do not silently strip it to make a model switch work.
- Use mock upstreams in tests. Paid live calls require explicit operator authorization.
- Keep supported protocols and untested desktop behavior explicit in the README.
