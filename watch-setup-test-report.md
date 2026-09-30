# /watch skill: YouTube cookie test report (2026-09-30)

| Step | Result | Key lines |
|---|---|---|
| 1. `WATCH_COOKIES_B64` set? | **FAIL** | `NOT SET`: the variable is empty or missing in this session's environment, and `~/.config/watch/` (where `.env` could provide it) does not exist. |
| 2. Decode header / count YouTube lines | **NOT RUN** | The auto-mode permission classifier blocked decoding the cookie variable ("Credential Materialization"). Because the variable is unset, the step would have had nothing to decode anyway. |
| 3. Tools + `setup.py --json` | **PASS** | `/usr/bin/ffmpeg`, `/usr/local/bin/yt-dlp`, `/usr/local/bin/deno` found; `"status": "ready"`, `"missing_binaries": []`, `"whisper_backend": "local"` |
| 4. YouTube end to end (`jNQXAC9IVRw`, efficient) | **FAIL** (exit=1, ~10s) | No cookies were used. `/tmp/w1.out` was empty (no Source/Duration/Transcript lines, 0 frame paths). stderr: `WARNING: ... HTTP Error 429: Too Many Requests`, then `ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies ...`, once at the metadata/captions step and again at the download step. `~/.config/watch/cookies.txt` does not exist, so there were no permissions or line count to check. |
| 5. Retry after 30s | **FAIL** (exit=1) | The same 429 and "Sign in to confirm you're not a bot" errors happened again at both steps. |

Leak check: the report contains no cookie data. The variable was unset, so no cookie content was ever available to this session.

**Overall verdict:** Not verified. `WATCH_COOKIES_B64` did not reach this session, and without cookies YouTube blocks the download with a bot check (HTTP 429). The tooling itself (ffmpeg, yt-dlp, deno, setup) is ready. Next step: add `WATCH_COOKIES_B64` to the environment's variables and re-run.
