# Hours plugin

Hours scores how skillfully you use AI coding assistants and shows you how to get better.

## Install

On the Hours site (https://hours-web.onrender.com), tick the box on the start page and click "Get my score". The page
gives you one line. Paste it into a terminal and press Enter. It adds this plugin to Claude Code
(`claude plugin marketplace add clawdaddy6767-png/hours-plugin`), installs it and connects this computer to your Hours
account.

## What it sends

Cleaned copies of your Claude Code, Codex, Gemini CLI, Antigravity and Copilot CLI sessions: once when the line
runs, then each time a Claude Code session starts or ends. Passwords, keys and tokens, ID, bank and card
numbers, health records, GPS coordinates and attachments are removed on your computer before anything is sent.
With each copy goes a text-free outline of the session file (record ids, times,
token counts and hashes) and the names of your AI config files (for example CLAUDE.md, your skill, command,
agent, hook and MCP server names), never their contents.

## Stop sending

    claude plugin uninstall hours@hours
