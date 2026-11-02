# Donna's workstation: the permanent work loop (Mac mini, user alliantoperator)

A LaunchAgent runs `~/Donna/donna_worker.sh` every minute while Donna's user is logged in. The script asks the
server for the next Alliant job (a cheap request; nothing happens when the queue is empty). When there is one, it starts
Claude Code with Chrome connected, and that one session works **the whole day in one Alliant tab**, the way a person
keeps Alliant open all day: each job, its report, then it waits for the next with `next_job.sh` (a quiet loop that
checks every 30 seconds, so waiting costs nothing), until 10 PM. Every Claude session gets its own Chrome tab group
and can't see the last one's tabs, so one session per day means one tab and one sign-in per day. Every run is
logged in `~/Donna/logs/<date>.log`. launchd never starts a second copy while one is still running, so she takes one
job at a time. "Donna pause" in Scott's private messages stops her taking jobs; a job lost for 30 minutes goes back in
the queue.

## Before installing

- Close the Claude Code window that's running the `/loop` (type `/exit`), so two loops don't take jobs.
- **Only Donna's Chrome may be connected to her Claude account.** A background run can't answer "Several Chrome
  browsers are connected: which one?". Either give Donna her own Claude account (sign in to it in Claude Code with
  `claude /login` and in the Claude extension in her Chrome), or sign the Claude extension out of the other browsers.
- Run `claude --chrome` once by hand and accept anything it asks (permissions, which browser), then `/exit`.

Her background runs load **none of her Claude account's connectors** (Gmail, Drive, Slack…):
`ENABLE_CLAUDEAI_MCP_SERVERS=false` in the script. An Alliant job only needs Chrome; a connector would let an
unattended run read her inbox (sign-in codes) or post as her.

## Install (paste in Terminal on the Mac mini, as Donna's user)

```
mkdir -p ~/Donna/logs ~/Library/LaunchAgents
cat > ~/Donna/next_job.sh <<'EOF'
#!/bin/bash
# Wait (quietly, up to 5 minutes) for Donna's next Alliant job and print it; prints NONE if there wasn't one.
# Her all-day session runs this between jobs, so waiting costs nothing; on NONE she nudges Alliant awake and runs it again.
KEY="$(cat "$HOME/.donna_worker_token")"
for i in $(seq 1 10); do
  JOB="$(curl -s --max-time 20 -H "Authorization: Bearer $KEY" https://chat.snowwhitelinen.com/api/donna/jobs/next)"
  case "$JOB" in *'"id"'*) echo "$JOB"; exit 0 ;; esac
  sleep 30
done
echo NONE
EOF
chmod +x ~/Donna/next_job.sh
cat > ~/Donna/donna_worker.sh <<'EOF'
#!/bin/bash
# Donna's Alliant work loop: launchd runs this every minute (deploy/donna_workstation.md in the Service Desk repo).
# One Claude session works the whole day in one Alliant tab; a new one starts only after the last one ends.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export BASH_DEFAULT_TIMEOUT_MS=600000 BASH_MAX_TIMEOUT_MS=600000   # her 5-minute waits for the next job
LOG="$HOME/Donna/logs/$(date +%F).log"
[ "$(date +%H)" -ge 22 ] && exit 0   # after 10 PM: no new session until morning (a job asked late waits for it)
JOB="$("$HOME/Donna/next_job.sh" | tail -1)"
case "$JOB" in *'"id"'*) ;; *) exit 0 ;; esac
echo "=== $(date) day session starts with job: $JOB" >> "$LOG"
cd "$HOME/Donna"
ENABLE_CLAUDEAI_MCP_SERVERS=false claude -p --chrome --permission-mode auto "You are Donna at your Alliant workstation, \
working the whole day in ONE Alliant tab, the way a person keeps Alliant open all day. Your first job is already taken \
for you: $JOB
Get your rules first: curl -s -H \"Authorization: Bearer \$(cat ~/.donna_worker_token)\" \
https://chat.snowwhitelinen.com/api/donna/jobs/rules . Follow them exactly, including reading your Alliant notes. \
Open Alliant once, in one tab, and keep it open: do the job, report it with the curl command in the rules, then wait \
for the next one by running ~/Donna/next_job.sh (it waits up to 5 minutes and prints the next job, or NONE). On NONE, \
keep Alliant awake: click once on the Alliant program window's own title bar (the strip across the top of the \
Alliant window with its name in it, inside the remote screen); never anywhere else, never a key. Then run it again. Never close the tab, never open a new one, never \
start over. Keep going until 10 PM Alaska time \
(check with: date), then disconnect from the page's bar above the remote screen and stop. Always report every job \
you take, even if you have to stop." >> "$LOG" 2>&1
echo "=== $(date) day session done (exit $?)" >> "$LOG"
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

## After a restart (or if she's stopped working)

1. **Log in as Donna** (user alliantoperator): at the Mac, or with LogMeIn (Chrome Remote Desktop only connects once
   someone is logged in). With auto-login on, the Mac does this itself.
2. **Chrome:** it opens at login (Login Items). Click the Claude icon and check it shows **Donna's** account.
3. **Alliant:** open the Windows App link, click Connect, sign in if asked ("Stay signed in? Yes"), and leave Alliant
   on Customer Center.
4. **Her loop and her Claude login**, in Terminal:
   ```
   launchctl list | grep donna; claude -p "reply with just: ok"
   ```
   A line ending in `com.donna.worker`, then `ok`. No line: `launchctl load ~/Library/LaunchAgents/com.donna.worker.plist`.
   Not logged in: `claude`, choose the subscription login, sign in as donna@snowwhitelinen.com, `/exit`.
5. **Not paused:** in Scott's private messages with @Donna, "Donna resume" (harmless if she wasn't paused).
6. **After a macOS update:** System Settings → General → Software Update → Automatic updates: "Install macOS updates"
   still off; Energy: prevent sleeping, start up after a power failure.
7. **Test:** ask Donna for a small Homer Golf Course change, say yes, and watch `tail -f ~/Donna/logs/$(date +%F).log`.

## Keep Alliant awake

Alliant (and the remote desktop around it) signs her out after a quiet stretch. Donna keeps it awake herself: while
she waits for jobs she checks every 5 minutes, and each time she clicks once on the Alliant program window's title
bar through her browser tool (nothing else: never a key, never inside the screen's fields or buttons). A Mac-side mouse nudge (osascript, CGEventPost) was tried 2026-11-02 and macOS
blocked it even with Terminal allowed under Accessibility, so it isn't used: `launchctl unload
~/Library/LaunchAgents/com.donna.keepawake.plist` removes it if it was installed.

## No screen saver, no sleep (Donna's user, no admin needed)

While the screen saver or a sleeping display is on, Chrome can stop drawing the remote desktop. Done 2026-11-02:

```
defaults -currentHost write com.apple.screensaver idleTime 0
cat > ~/Library/LaunchAgents/com.donna.awake.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.donna.awake</string>
  <key>ProgramArguments</key><array><string>/usr/bin/caffeinate</string><string>-dims</string></array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.donna.awake.plist
```
