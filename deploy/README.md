# Running the Service Desk on Amazon Lightsail

The desk needs a machine that stays on and can hold a live connection to Slack (`--listen`, Slack Socket Mode).
A small Lightsail server does that. The scripts here set it up as a service that starts on boot and restarts itself
if it crashes.

## 1. Create the server (Lightsail console, about 5 minutes)

1. Go to https://lightsail.aws.amazon.com and sign in (or create an AWS account).
2. **Create instance** → Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**.
3. Pick the smallest plan with **1 GB** of memory. Name it `service-desk`. **Create instance**.
4. When it shows *Running*, click the terminal icon (**Connect using SSH**). A terminal opens in the browser.
   Everything below is typed there.

No firewall changes are needed: the desk only connects out to Slack and Anthropic.

## 2. Give the server read access to the code

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 && cat ~/.ssh/id_ed25519.pub
```

Copy the line it prints. On GitHub open the repository → **Settings** → **Deploy keys** → **Add deploy key**,
paste it, title it `service-desk server`, leave *Allow write access* off, **Add key**. Then:

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone -b claude/admiring-planck-rdk120 git@github.com:woodtang2000/repository.git Repository
cd Repository && sudo bash deploy/setup.sh
```

## 3. Tokens

```bash
sudo nano /etc/service-desk.env
```

Replace the three `...` values with the same tokens the cloud sessions use: `SLACK_BOT_TOKEN` (xoxb-),
`SLACK_APP_TOKEN` (xapp-) and `SERVICE_DESK_API_KEY`. Save with Ctrl+O, Enter, Ctrl+X.

## 4. Customer data

Download `Alliant Customer Record Cards.pdf` from Dropbox to your computer. In the browser terminal, use the
gear/upload button (**Upload file**) to send it to the server; it lands in the home folder. Then:

```bash
cd ~/Repository && bash deploy/refresh_data.sh ~/"Alliant Customer Record Cards.pdf"
```

It should report about 866 accounts. Do the same whenever the PDF is updated; the desk picks the new data up
within the hour.

## Automatic data updates from Dropbox (optional)

The server can check Dropbox every hour and rebuild the data whenever the Record Cards PDF changes.

1. Go to https://www.dropbox.com/developers/apps → **Create app** → **Scoped access** → **App folder**.
   Name it e.g. `SWL Service Desk` → **Create app**. (App folder access means it can only see
   `Dropbox/Apps/SWL Service Desk/`, nothing else in your Dropbox.)
2. **Permissions** tab: tick `files.metadata.read` and `files.content.read` → **Submit**.
3. Save the Alliant export as `Alliant Customer Record Cards.pdf` in `Dropbox/Apps/SWL Service Desk/`.
4. On the server: `cd ~/Repository && python3 deploy/dropbox_sync.py setup`. It asks for the **App key** and
   **App secret** (app's **Settings** tab), gives you a link to click **Allow**, then asks for the code Dropbox
   shows. It installs the hourly check and runs the first one.

From then on, replacing the PDF in that folder is all it takes; the desk has the new data within the hour.
Check what it did with `tail ~/dropbox_sync.log`.

## Silent review before go-live (optional)

`sudo bash deploy/review_setup.sh` runs `bot.py` at 5 past every hour: it reads the real `#route-N` channels the
bot is in and posts what it would have done to `#service-desk-review`. Drivers see nothing. React 👎 on anything
wrong. `sudo bash deploy/review_setup.sh off` stops it.

## Customer texting (optional)

Customers text a Twilio number; each number is pinned to its account(s) in
`service_changes/data/customer_phones.csv` (`phone,accounts,name`, accounts separated by `;`). The bot only ever
sees that number's accounts, so a customer can't ask about or change anyone else's. Tickets land in the office
channel marked as a customer text; ✅ texts the readback back.

A number that isn't on the list is asked for a name and business; the office gets a card in the office channel
with the best-matching account and approves it with ✅. Office can also type `add 907-555-1234 to <account or
name>` or `remove 907-555-1234` there; drivers can type `add …` in their route channel, which sends the office a
card to approve. Nothing about any account goes to a number until the office has approved it.

Put `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_NUMBER` in `/etc/service-desk.env`, then
`sudo bash deploy/sms_setup.sh`. Log: `journalctl -u service-desk-sms -f`. Stop: `sudo bash deploy/sms_setup.sh off`.

## 5. Start it

```bash
sudo systemctl restart service-desk
journalctl -u service-desk -f
```

You should see `listening`. Post in `#route-12-test`: the ⏳ should appear within a second or two.
Ctrl+C stops watching the log, not the desk.

## Day to day

| To | Run |
|---|---|
| See what it is doing | `journalctl -u service-desk -f` |
| Last Dropbox data update | `tail ~/dropbox_sync.log` |
| Get the latest code | `cd ~/Repository && bash deploy/update.sh` |
| Stop it | `sudo systemctl stop service-desk` |
| Go live | `sudo nano /etc/service-desk.env`: set `DESK_CHANNEL`, `ROUTE_CHANNELS` (e.g. `route-1,route-2`) and `SERVICE_DESK_LIVE=1`, then `sudo systemctl restart service-desk` |

Turn off the cloud-session desk and the scheduled "Service Desk silent trial" routines once this is running, so
two copies are not answering the same channels.
