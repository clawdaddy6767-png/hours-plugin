---
description: Connect this computer to your Hours account and send your cleaned AI coding sessions (for a computer that already has the plugin)
argument-hint: <site-address> <pairing-code> [repair]
disable-model-invocation: true
---

The user wants to connect this computer to Hours. They copied this line from the Hours connect page in their browser, where they already accepted the collection notice. Pairing refuses the code without that consent.

Site address: `$0`
Pairing code: `$1`
Third word (optional): `$2`

Follow these steps exactly. Do not run any other command, and do not read, open or summarize any transcript file.

1. Check both values before running anything.
   - The site address must match `^https://[a-z0-9.-]+(:[0-9]+)?/?$`, or for local testing `^http://(localhost|127\.0\.0\.1)(:[0-9]+)?/?$`.
   - The pairing code must match `^HRS-[A-Z2-9]{4}-[A-Z2-9]{4}$`.
   - If either check fails, stop. Tell the user: "That line does not look right. Copy the whole line from the Hours page and paste it again." Run nothing.

2. Pair this computer and send. Run this one command with the Bash tool, putting the two checked values inside the single quotes:

   ```
   /usr/bin/python3 -I -B "${CLAUDE_PLUGIN_ROOT}/scripts/hours_collect.py" --data-dir "${CLAUDE_PLUGIN_DATA}" --pair '<PAIRING CODE>' --endpoint '<SITE ADDRESS>' --yes --detach
   ```

   It wakes the site, redeems the code and counts the sessions on this computer (Claude Code, Codex, Gemini CLI, Antigravity and Copilot CLI). Then it cleans them on this computer and sends only the cleaned copy, in a background process, so the command returns at once.

   Add ` --repair` to the end of that command only when the third word is exactly `repair`, or when the step below tells you to after the user said yes.

3. If the command's last JSON line has `"error": "already_paired"`, this computer is already connected to an Hours account (the plain-text lines name it). The pairing code was not used. Show the user those lines and ask: "Switch this computer to the new account?" Do nothing else until they answer. If they say yes, run the same command again with ` --repair` added. If they say no, stop; this computer stays as it was. Never add `--repair` without a clear yes from the user in this chat.

4. Show the user the plain-text lines the command printed (not the JSON lines). If it failed, show its message and stop. Otherwise tell the user their score appears on the Hours page in their browser, and that new sessions are sent each time a Claude Code session starts or ends.
