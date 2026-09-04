# email-infra skills

Skills shared across this repo. Each one is a direct child directory holding a
`SKILL.md`:

```
.claude/skills/<skill-name>/SKILL.md
```

Claude Code auto-discovers everything here whenever it runs anywhere inside the
repo (the launch directory and all parents up to the repo root are scanned). A
loose `SKILL.md` sitting in a normal folder is **not** a skill — it is inert.

## Current skills

- **`smartlead-orphaned-thread-reply`** — A lead replied, but the inbox that
  sent to them was burned/swapped and deleted, so SmartLead shows "Email account
  ... has been removed" and the thread is permanently read-only. Explains why
  "Reallocate mailboxes" does not fix it (it only redistributes the unsent
  queue) and sends the reply from a live inbox with the original
  `In-Reply-To`/`References`, so it still lands in the same conversation.
  Engine: `reply_in_thread.py`.
