## Plan Review Summary
The plan is highly feasible, exceptionally well-structured, and accurately reflects the current state of the codebase. The identified issues—such as the synchronous I/O blocks in `src/proxy_app/main.py` lifespan and `src/rotator_library/usage/persistence/storage.py`, the `HTTPException` swallowing in `/v1/token-count`, the O(N) cache miss amplification in `ProviderCache`, and the embedding batch usage overcounting—are all verified present in the codebase. The phased rollout strategy (M1-M7) and feature flag recommendations provide a safe path for implementation without risking endpoint regressions.

## Critical Issues
*None found in the plan itself.* The plan correctly identifies critical issues in the codebase and proposes sound solutions.

## Important Issues
- **`asyncio.to_thread` usage (C1):** In `src/rotator_library/utils/resilient_io.py`, the `_writer.write()` method uses a synchronous `flock` and standard `open()`. When migrating the `save()` method in `storage.py` off the event loop, simply wrapping `self._writer.write(data)` in `asyncio.to_thread()` is the safest approach without needing to rewrite the resilient disk I/O library to be natively async. The plan mentions this, but it's worth emphasizing to prevent scope creep.

## Suggestions
- **FastAPI CORS default migration (D2):** When shifting from `allow_origins=["*"]` to an explicit allowlist in `src/proxy_app/main.py`, ensure that `http://localhost:*` and `http://127.0.0.1:*` are included in the default fallback if the environment variable is not explicitly set. This prevents breaking local UI/TUI developer workflows out of the box.
- **Dependency Separation (F3):** When splitting `requirements.txt` into runtime and dev dependencies, be careful not to remove `rich` or `prompt_toolkit` from runtime if they are used by the CLI/TUI entry points (which they appear to be in `main.py` onboarding).

## Codebase Alignment
- **A1 (`/v1/token-count` fix):** -> Aligns perfectly with `src/proxy_app/main.py:1497-1520` (exception block catches `HTTPException` and re-raises as 500).
- **A2 (Embedding usage):** -> Aligns perfectly with `src/proxy_app/batch_manager.py` (usage object is attached to every single batch item unconditionally).
- **B1 (Streaming wrapper):** -> Aligns perfectly with `src/proxy_app/main.py:717` (`streaming_response_wrapper` builds `response_chunks` regardless of logging mode).
- **B3 (ProviderCache amplification):** -> Aligns perfectly with `src/rotator_library/providers/provider_cache.py:382` (spawns un-deduplicated `_check_disk_fallback` tasks on every miss).
- **C1 (Async I/O):** -> Aligns perfectly with `main.py`'s `lifespan` function (sync `json.load`) and `storage.py`'s `save` function (sync `write()`).
- **D1/D2 (Security defaults):** -> Aligns perfectly; API key is printed plaintext at `main.py:96`, and CORS uses `["*"]` with `allow_credentials=True` at `main.py:666`.

## Approval Status
**READY TO IMPLEMENT**
