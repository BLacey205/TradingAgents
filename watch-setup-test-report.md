# /watch skill: YouTube-with-cookies test report

Date: 2026-09-30. Observe-only run. Nothing was installed or changed. No cookie values were printed or logged.

## Step 1: WATCH_COOKIES_B64 present: **FAIL**
```
NOT SET
```
The environment variable is not set in this session's container.

## Step 2: Binaries and setup: **PASS**
```
/usr/bin/ffmpeg
/usr/local/bin/yt-dlp
/usr/local/bin/deno
  "status": "ready",
  "missing_binaries": [],
  "whisper_backend": "local",
```
Exit code 0.

## Step 3: YouTube end to end (jNQXAC9IVRw): **FAIL**
- `exit=1`, real 0m10.0s
- /tmp/w1.out was empty, so there are no Source/Duration/Transcript header lines, no transcript and 0 frame paths.
- /tmp/w1.err (filtered):
```
[watch] working dir: /tmp/w1
[watch] checking metadata/captions via yt-dlp…
WARNING: [youtube] jNQXAC9IVRw: Unable to download webpage: HTTP Error 429: Too Many Requests
WARNING: [youtube] No title found in player responses; falling back to title from initial data. Other metadata may also be missing
ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies for the authentication. ...
[watch] downloading video via yt-dlp…
WARNING: [youtube] jNQXAC9IVRw: Unable to download webpage: HTTP Error 429: Too Many Requests
WARNING: [youtube] No title found in player responses; ...
ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. ...
```
- Cookie file:
```
stat: cannot statx '/root/.config/watch/cookies.txt': No such file or directory
wc: /root/.config/watch/cookies.txt: No such file or directory
```

## Step 4: Frame inspection: **SKIPPED**
Step 3 did not succeed, so there were no frames to look at.

## Step 5: Retry after 30s: **FAIL**
`exit=1`, real 0m5.6s. The output was the same as step 3: HTTP 429, then "Sign in to confirm you’re not a bot".

## Overall verdict
**YouTube does not work in this run.** WATCH_COOKIES_B64 was not set, so no cookie file was written, and YouTube blocked the anonymous requests (429 / bot check). The tooling itself is ready. This run did not test cookie-based download at all. To test it, the variable has to be present in the environment.
