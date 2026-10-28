# Donna's workstation: the permanent work loop (Mac mini, user alliantoperator)

A LaunchAgent runs `~/Donna/donna_worker.sh` every minute while Donna's user is logged in. The script asks the
server for the next Alliant job (a cheap request; nothing happens when the queue is empty). When there is one, it starts
Claude Code just for that job, with Chrome connected; she does it and reports it, and the session ends. Every run is
logged in `~/Donna/logs/<date>.log`. launchd never starts a second copy while one is still running, so she takes one
job at a time. "Donna pause" in Scott's private messages stops her taking jobs; a job lost for 30 minutes goes back in
the queue.

## Before installing

- Close the Claude Code window that's running the `/loop` (type `/exit`), so two loops don't take jobs.
- **Only Donna's Chrome may be connected to her Claude account.** A background run can't answer "Several Chrome
  browsers are connected: which one?". Either give Donna her own Claude account (sign in to it in Claude Code with
  `claude /login` and in the Claude extension in her Chrome), or sign the Claude extension out of the other browsers.
- Run `claude --chrome` once by hand and accept anything it asks (permissions, which browser), then `/exit`.

## Install (paste in Terminal on the Mac mini, as Donna's user)

```
mkdir -p ~/Donna/logs ~/Library/LaunchAgents
cat > ~/Donna/donna_worker.sh <<'EOF'
#!/bin/bash
# Donna's Alliant work loop: launchd runs this every minute (deploy/donna_workstation.md in the Service Desk repo).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOG="$HOME/Donna/logs/$(date +%F).log"
KEY="$(cat "$HOME/.donna_worker_token")"
JOB="$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" https://chat.snowwhitelinen.com/api/donna/jobs/next)" || exit 0
case "$JOB" in ""|*'"job": null'*) exit 0 ;; esac
echo "=== $(date) job: $JOB" >> "$LOG"
cd "$HOME/Donna"
claude -p --chrome --permission-mode auto "You are Donna at your Alliant workstation. This job is already taken from \
your queue for you (don't call jobs/next): $JOB
Get your rules first: curl -s -H \"Authorization: Bearer \$(cat ~/.donna_worker_token)\" \
https://chat.snowwhitelinen.com/api/donna/jobs/rules . Follow them exactly, including reading your Alliant notes. \
Do the job in the Alliant tab in your Claude tab group (open it there if your group has none, as the rules say), then \
report the result with the curl command in the rules. Always finish with a report, even if you have to stop." >> "$LOG" 2>&1
echo "=== $(date) done (exit $?)" >> "$LOG"
EOF
chmod +x ~/Donna/donna_worker.sh
cat > ~/Library/LaunchAgents/com.donna.worker.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.donna.worker</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$HOME/Donna/donna_worker.sh</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Donna/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Donna/logs/launchd.log</string>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.donna.worker.plist && echo "Donna's loop is running"
```

## Check on her

```
tail -40 ~/Donna/logs/$(date +%F).log      # today's jobs and what she did
launchctl list | grep com.donna.worker     # running? (a line with com.donna.worker)
```

Stop: `launchctl unload ~/Library/LaunchAgents/com.donna.worker.plist`. Start again: the same with `load`.
