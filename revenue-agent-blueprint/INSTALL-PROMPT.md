# Install Prompt

Paste this into a Claude Code session running **at the root of the project where you want the Revenue Agent Team to live** (it does not need to be this repo — most people run it from a clean folder or their actual business's project). Claude will do the file copying for you; it cannot restart itself, so that one step still comes back to you (see below).

---

```
Install the Revenue Agent Blueprint into this project's root.

The blueprint lives in a `revenue-agent-blueprint/` folder (either already
somewhere in this repo, or tell me its path if you can't find it — ask me
rather than guessing). Inside it: `.claude/agents/` has four agent files
(demand-research.md, offer-architect.md, content-angle.md,
conversion-path.md) and `.claude/skills/revenue-system/` has the
coordinator skill.

Do this:
1. Create `.claude/agents/` and `.claude/skills/` at this project's root
   if they don't already exist.
2. Copy the four agent files into the root `.claude/agents/`. If any of
   those filenames already exist there with different content, stop and
   show me the conflict instead of overwriting.
3. Copy the `revenue-system/` skill folder into the root
   `.claude/skills/`, same conflict rule.
4. Also copy `BRIEF-TEMPLATE.md` to the project root if it isn't already
   there.
5. List what you copied and confirm the four agent files plus the skill
   now exist at the project root.
6. Remind me that I need to fully restart Claude Code before any of this
   will work — you cannot do that step yourself.

Do not modify any other files in this project.
```

---

That's the whole prompt — copy everything inside the fenced block above.
