# Install Prompt

Unzip the Blueprint into a brand-new empty folder, open a Claude Code session in that folder, and paste everything inside the fenced block below as your first message. Claude will move the files into place and then tell you, verbatim, exactly how to close and restart — that one step is yours; no prompt can do it for you.

---

```
Set up the Revenue Agent Blueprint in this project.

Look in the current directory for a `revenue-agent-blueprint` folder (if
you can't find one, ask me where it is — don't guess). Inside it you'll
find:
- .claude/agents/demand-research.md
- .claude/agents/offer-architect.md
- .claude/agents/content-angle.md
- .claude/agents/conversion-path.md
- .claude/skills/revenue-system/SKILL.md
- BRIEF-TEMPLATE.md

Do this, in order:
1. Create `.claude/agents/` and `.claude/skills/` at the root of this
   project (the directory this session is running in) if they don't
   already exist.
2. Copy the four agent files into the root `.claude/agents/`. If a file
   with the same name already exists there and its content differs,
   stop and show me the conflict instead of overwriting it.
3. Copy the `revenue-system/` skill folder into the root
   `.claude/skills/`, with the same conflict check.
4. Copy `BRIEF-TEMPLATE.md` to the project root if it isn't already
   there.
5. List every file you copied, with its final path, so I can see
   exactly what changed.
6. Confirm all four agent files and the skill now exist at the project
   root.
7. Stop there. Do not try to run, test, or invoke the agents or the
   skill in this session — they are not loaded yet and nothing you do
   right now will prove otherwise.

Do not modify any other files in this project.

Once steps 1-6 are done, say exactly this back to me and nothing else
after it:

"Setup is done, but none of it is active yet. Subagents and skills are
only read when Claude Code starts up, so this running session still
only knows the old file list — that's not a bug, it's just how it
works. To finish:
1. Close this session completely. In a terminal: exit the process
   (Ctrl+D or type `exit`), don't just start a new chat inside it. In
   the desktop app: quit the app, don't just close the window.
2. Reopen Claude Code in this same folder.
3. Run /agents and confirm you see demand-research, offer-architect,
   content-angle, and conversion-path listed.
4. Only after that, give it your idea and ask it to run the
   revenue-system skill."
```

---

That's the whole prompt — copy everything inside the fenced block above, including the quoted message at the end.
