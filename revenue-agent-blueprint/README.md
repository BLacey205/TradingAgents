# The Revenue Agent Blueprint

Four single-job Claude Code subagents, chained by a coordinator, that turn one idea into positioning, an offer, a content angle, and a lead funnel — in one sitting.

This is infrastructure to install, not a PDF to read.

## What's inside

```
revenue-agent-blueprint/
├── BRIEF-TEMPLATE.md              ← fill this in with your one idea
├── .claude/
│   ├── agents/
│   │   ├── demand-research.md     ← Agent 1: is there real demand?
│   │   ├── offer-architect.md     ← Agent 2: positioning + priced offer ladder
│   │   ├── content-angle.md       ← Agent 3: the video/content hook and structure
│   │   └── conversion-path.md     ← Agent 4: lead magnet + email sequence
│   └── skills/
│       └── revenue-system/
│           └── SKILL.md           ← the coordinator that chains all four
└── output/                        ← each run lands here as output/<your-idea-slug>/
```

Each agent does one job and hands its output file to the next — that's the whole mechanism. No agent duplicates another's work, and no agent guesses at a stage it hasn't been given the inputs for.

## Install (3 minutes)

1. Copy the `.claude/` folder from this directory into the root of the project or folder where you run Claude Code (or into `~/.claude/` to make it available everywhere).
2. **Restart Claude Code.** This is the step almost everyone forgets — subagents and skills are only loaded at startup, so if you skip this, the team won't show up.
3. Copy `BRIEF-TEMPLATE.md` too if you want the guided input format, though free-text is fine — see below.

## Run it

Fill in `BRIEF-TEMPLATE.md` with your one idea, or just describe it in chat, then ask Claude Code to run the Revenue Agent Team — e.g.:

> Run my idea through the revenue-system skill: [paste your idea, or the path to your filled-in brief]

Watch the terminal. Each agent runs for real, reads the previous agent's actual output file, and writes its own. Nothing here is pre-computed — the same idea run twice will produce different (and improving, as you refine the brief) results.

When it's done, you'll have `output/<your-idea-slug>/revenue-system.md` — one file with your validated demand research, your priced offer ladder, your content angle, and your lead-generation funnel, ready to hand to whoever builds the video, landing page, or emails next.

## Why four agents instead of one prompt

A single generalist prompt asked to "research my idea and build me a funnel" tends to skip straight to generic output — it hasn't actually looked at real demand signals, so the offer is guessed, the content angle is guessed, and the funnel is guessed on top of two guesses. Splitting the work into four single-job agents that must read each other's actual output forces every downstream stage to be built on the stage before it, not on a vibe. The same reason a real team outperforms one overworked generalist.
