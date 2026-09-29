# Master Prompt: Install and Set Up the claude-video `/watch` Skill

Paste everything below the line into a new Claude Code session (cloud or local). It explains what
happened last time, what the end state should be, and what to do if any step fails.

---

## Context: what was done before

In an earlier Claude Code cloud session on the repo `blacey205/tradingagents`, the user asked to
"install the claude-video skill from github.com/bradautomates/claude-video". This is what happened:

1. **Cloned the source repo.** Ran `git clone --depth 1 https://github.com/bradautomates/claude-video`
   into a scratch directory. The skill itself lives in `skills/watch/` and contains:
   - `SKILL.md`: the skill definition (name `watch`, invoked as `/watch <url-or-path> [question]`)
   - `scripts/`: `watch.py` (entry point), `download.py` (yt-dlp), `frames.py` (ffmpeg frames),
     `transcribe.py`, `whisper.py` (Groq/OpenAI Whisper clients), `setup.py` (preflight and installer),
     `config.py`, and `build-skill.sh` (a packaging script the skill doesn't need at runtime)
   - The repo also ships a Claude Code plugin wrapper (`.claude-plugin/`), plus a `SessionStart` hook
     (`hooks/`) that only prints a one-line setup status.
2. **Reviewed the scripts before installing.** They only call `yt-dlp`, `ffmpeg` and `ffprobe` locally
   through `subprocess`. The only outside services are `api.groq.com` and `api.openai.com`, used only
   for Whisper transcription when a video has no captions. Nothing suspicious was found.
3. **Installed at the project level.** A cloud container is thrown away after each session, so the
   skill was copied into the repo instead of the user's home directory:
   - `.claude/skills/watch/SKILL.md`
   - `.claude/skills/watch/scripts/*.py` (with `build-skill.sh` left out)
   - `.claude/skills/watch/LICENSE` (MIT, copied from upstream)
   - The plugin's `SessionStart` hook was left out because it's cosmetic.
   - Source version: upstream commit `83da59f` (skill version 0.2.0).
4. **Committed and pushed** to the branch `claude/install-claude-video-skill-g048ts` with the message
   "Add claude-video /watch skill". Claude Code picked the skill up immediately: `watch` appeared in
   the list of available skills.
5. **Installed the system tools** at the user's request:
   `apt-get install -y ffmpeg && pip install yt-dlp`. This gave ffmpeg 6.1.1 and yt-dlp 2026.08.19.
6. **Ran the skill's setup once:** `python3 .claude/skills/watch/scripts/setup.py`. This created
   `~/.config/watch/.env` with owner-only permissions (0600).
   - Result of `setup.py --json`: `missing_binaries: []`, `status: needs_key`, `first_run: true`.
   - The only thing left was an **optional** Whisper API key. Without one, `/watch` still works on
     any video that has captions. Videos without captions come back as frames only.

### Final state
- Skill files are committed in the repo on branch `claude/install-claude-video-skill-g048ts`.
  They reach `main` only after that branch is merged.
- ffmpeg, yt-dlp and `~/.config/watch/.env` existed only in that one container. They must be
  reinstalled in every new cloud session unless they're added to the environment's setup script.
- No Whisper key has been configured yet.

---

## Task for this session

Check that the `/watch` skill is installed and working, and repair whatever is missing. Work through
the levels below in order. At each level, run the check first. Only if the check fails, try the fixes
in the order listed and stop at the first one that works. Report what you found and what you changed.

### Level 1: Skill files are present
**Check:** `ls .claude/skills/watch/SKILL.md .claude/skills/watch/scripts/watch.py`

**If they're missing:**
- **A.** The branch may not be merged. Run `git fetch origin claude/install-claude-video-skill-g048ts`,
  then `git checkout origin/claude/install-claude-video-skill-g048ts -- .claude/skills/watch`.
- **B.** Reinstall from upstream:
  `git clone --depth 1 https://github.com/bradautomates/claude-video /tmp/cv`, then
  `mkdir -p .claude/skills/watch && cp -r /tmp/cv/skills/watch/{SKILL.md,scripts} .claude/skills/watch/ && cp /tmp/cv/LICENSE .claude/skills/watch/`,
  then `rm -f .claude/skills/watch/scripts/build-skill.sh`. Commit and push.
- **C.** If `git clone` from GitHub is blocked by network policy, download the tarball instead:
  `curl -L https://codeload.github.com/bradautomates/claude-video/tar.gz/refs/heads/main | tar xz -C /tmp`.
  Or use the GitHub MCP tools (`get_file_contents` on `skills/watch/...`) and write each file.
- **D.** If upstream moved or changed its layout, search the cloned repo with
  `find . -name SKILL.md` for the folder whose `name:` is `watch`. If the folder isn't found, check
  the upstream releases page for a `watch.skill` file. It's a zip archive, so unzip it into
  `.claude/skills/watch/`.
- **E.** If none of these work in a cloud session, run `read_documentation` with topic
  `environment.network` and tell the user which host needs to be allowed.

### Level 2: Claude Code recognizes the skill
**Check:** `watch` appears in the list of available skills, or `/watch` works as a command.

**If it's not recognized:**
- **A.** Make sure `SKILL.md` starts with valid frontmatter: `---`, then `name: watch`, then
  `description: ...`, then `---`.
- **B.** Skills are loaded when a session starts. Start a new session, or in a local CLI, restart
  `claude`.
- **C.** Install it for your user instead of the project: copy the folder to `~/.claude/skills/watch/`.
  This works locally only; cloud containers don't keep it.
- **D.** On a local machine, use the upstream plugin route:
  `/plugin marketplace add bradautomates/claude-video`, then `/plugin install watch@claude-video`.
- **E.** For other agent hosts (Codex, Cursor, Gemini CLI): `npx skills add bradautomates/claude-video -g`.
- **F.** For claude.ai on the web: download `watch.skill` from the latest upstream release, then go to
  Settings → Capabilities → Skills → `+`.

### Level 3: System tools (ffmpeg, ffprobe, yt-dlp)
**Check:** `python3 .claude/skills/watch/scripts/setup.py --json`. The `missing_binaries` list should
be `[]`.

**If tools are missing:**
- **A.** `apt-get install -y ffmpeg && pip install yt-dlp`
- **B.** If `apt-get install` can't find the package: run `apt-get update` first, then retry. Add
  `sudo` if you aren't root.
- **C.** If apt is unavailable or blocked, get a static ffmpeg build. Try `pip install imageio-ffmpeg`
  and symlink its binary as `ffmpeg`, or download a static build from johnvansickle.com/ffmpeg into
  `~/.local/bin`. A pip-installed ffmpeg may not include `ffprobe`, so confirm with
  `which ffmpeg ffprobe`.
- **D.** If `pip install` fails with an "externally managed environment" error, use one of these:
  `pip install --user yt-dlp`, `pipx install yt-dlp`, or `pip install --break-system-packages yt-dlp`.
  Or download the single-file binary:
  `curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o ~/.local/bin/yt-dlp && chmod +x ~/.local/bin/yt-dlp`.
- **E.** On macOS: `brew install ffmpeg yt-dlp`. Running `setup.py` without flags also installs them
  automatically there. On Windows: `winget install ffmpeg yt-dlp`, and use `python` instead of
  `python3`.
- **F.** If PyPI or apt is blocked by network policy, run `read_documentation` with topic
  `environment.network` and tell the user.
- **G.** To make the tools permanent in cloud sessions, the user should add
  `apt-get install -y ffmpeg && pip install yt-dlp` to the environment's **setup script**. See
  `read_documentation` with topic `environment.setup_script`. Another option is a SessionStart hook
  (the `session-start-hook` skill).

### Level 4: Config file and first-run setup
**Check:** `ls -l ~/.config/watch/.env`. It should exist with 600 permissions.

**If it's missing or wrong:**
- **A.** `python3 .claude/skills/watch/scripts/setup.py` creates the file and is safe to run more
  than once.
- **B.** If the permissions are wrong: `chmod 600 ~/.config/watch/.env`
- **C.** To finish setup without a key, so the skill stops asking about it, add these lines to the
  file. Put no comments on the same line as a value:
  `WATCH_DETAIL=balanced`
  `SETUP_COMPLETE=true`
  The allowed values for `WATCH_DETAIL` are `transcript`, `efficient`, `balanced` (recommended) and
  `token-burner`.

### Level 5: Whisper API key (optional)
**Check:** `setup.py --json` shows `has_api_key: true`.

**If there's no key:**
- **A.** Ask the user for a Groq key (preferred: cheaper and faster; get one at
  console.groq.com/keys) or an OpenAI key (platform.openai.com/api-keys). Write it to
  `~/.config/watch/.env` as `GROQ_API_KEY=...` or `OPENAI_API_KEY=...`.
- **B.** To keep the key across cloud sessions, add it as an environment secret instead. See
  `read_documentation` with topic `environment.secrets`. The skill reads keys from environment
  variables too.
- **C.** If the user doesn't want a key, run with `--no-whisper`. Videos without captions will come
  back as frames only.
- **D.** If calls to `api.groq.com` or `api.openai.com` are blocked by network policy, switch to the
  other provider, or fall back to C.

### Level 6: End-to-end test
**Check:** `python3 .claude/skills/watch/scripts/setup.py --check` exits with code 0. Then try it on a
real video, for example `/watch https://youtu.be/dQw4w9WgXcQ what happens at 0:30?`. You can also run
`python3 .claude/skills/watch/scripts/watch.py "<url>" --start 0:25 --end 0:35`.

**If the test fails:**
- **A.** If YouTube blocks the download (bot check, HTTP 403): update yt-dlp with
  `pip install -U yt-dlp`, since YouTube changes often.
- **B.** Still blocked, which is common from cloud IP addresses: test with a local file instead.
  Create a test video with
  `ffmpeg -f lavfi -i testsrc=duration=10:size=640x360:rate=30 /tmp/test.mp4` and run `/watch /tmp/test.mp4`.
- **C.** If the site isn't YouTube, confirm yt-dlp supports it with `yt-dlp --list-extractors`. If
  it doesn't, download the video some other way and pass the local path.
- **D.** If frame extraction fails, re-check `ffprobe` (Level 3C) and try `--detail efficient`.
- **E.** If a download is cut off or TLS fails through the cloud proxy, read `/root/.ccr/README.md`
  and check the proxy status as described there. Never turn off TLS verification.
- **F.** If the output is too large for the context window, use `--detail transcript` or
  `--detail efficient`, or narrow the time range with `--start` and `--end`.

### Finishing up
- Commit only changes to repo files (`.claude/skills/watch/**`) on the designated branch. Never commit
  `~/.config/watch/.env` or any API key.
- Tell the user which levels passed, what you fixed, and what they still need to do: merging the
  branch, adding the setup-script line, and optionally adding the Whisper key as a secret.
