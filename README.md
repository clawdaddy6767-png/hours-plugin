# Hours plugin

Hours scores how skillfully you use AI assistants and shows you how to get better. The plugin installs into
Claude Code, but it reads every supported AI tool on your computer, not only Claude Code.

## Tools it reads

Claude Code, Claude Cowork, Codex, Gemini CLI, Antigravity, Copilot CLI, Cursor, OpenCode, Pi, Hermes, OpenClaw,
NanoClaw, Aider, Cline, Roo Code, Continue, Goose, Qwen Code and Zed.

Chat apps (claude.ai, ChatGPT, Gemini and Microsoft Copilot) are added on the Hours site by uploading their data
export.

## Install

On the Hours site (https://hours-web.onrender.com), tick the box on the start page and click "Get my score". The page
gives you one line. Paste it into a terminal and press Enter. It adds this plugin to Claude Code
(`claude plugin marketplace add clawdaddy6767-png/hours-plugin`), installs it and connects this computer to your Hours
account. On a computer without Claude Code, the same line runs the collector directly.

## What it sends

Cleaned copies of your sessions from the tools above: once when the line runs, then each time a Claude Code
session starts or ends. Passwords, keys and tokens, ID, bank and card
numbers, health records, GPS coordinates and attachments are removed on your computer before anything is sent.
With each copy goes a text-free outline of the session file (record ids, times,
token counts and hashes) and the names of your AI config files (for example CLAUDE.md, your skill, command,
agent, hook and MCP server names), never their contents.

## Stop sending

    claude plugin uninstall hours@hours
