# Deploying on Windows Server

Setup guide for running the LLM Visibility Tracker on a Windows Server box,
reachable from other machines, and surviving a reboot.

The app is a Streamlit UI that drives a real Chrome browser to scrape
ChatGPT, Perplexity, Gemini and Google. The browser part is what makes this
different from deploying an ordinary web app, and it is where most of the
setup effort goes.

Read the **Terms of Service** note in `README.md` before deploying. Scraping
these chat UIs breaches their ToS; run it from accounts and IPs you are
willing to lose.

---

## 1. What the machine needs

| | Requirement | Why |
|---|---|---|
| OS | Windows Server 2019 or 2022 | Tested target |
| Python | **3.11 or 3.12** | 3.13 has no Scrapling wheel yet |
| Google Chrome | Current stable, **installed machine-wide** | Scrapers launch `channel="chrome"`; the bundled Chromium is a fallback and fingerprints worse |
| RAM | 4 GB minimum, 8 GB comfortable | Each scrape holds a Chrome profile open |
| Disk | ~3 GB | Browser binaries plus the SQLite results DB |
| Outbound | 443 to the LLM sites, and to your proxy endpoints | |

Install Python from python.org, **ticking "Add python.exe to PATH"**. The
Microsoft Store build puts packages somewhere Streamlit cannot always find
and is best avoided.

Confirm before going further:

```powershell
python --version          # 3.11.x or 3.12.x
where.exe chrome          # should print a path under Program Files
```

---

## 2. Get the code

```powershell
cd C:\Users\Administrator\Desktop
git clone <your-repo-url> LLM-Visibility-Tracker
cd LLM-Visibility-Tracker
```

Everything below assumes that folder. If you put it elsewhere, change the
paths in the scheduled-task section too.

---

## 3. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

python -m playwright install chromium
scrapling install
```

If `Activate.ps1` is blocked, PowerShell's execution policy is the cause:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

`patchright` in requirements.txt is optional — a patched Playwright the
scrapers try first because it is harder to fingerprint. If its install
fails, delete that line and reinstall; `scrapers/_browser.py` falls back to
plain Playwright on its own.

---

## 4. Configure

Two files, both gitignored, both created by hand on the server.

**`.env`** — proxy credentials:

```
PROXY_USER=your-user
PROXY_PASS=your-pass
```

Copy `.env.example` and fill it in. These are read by
`scrapers/_browser.py`, which parses the file itself — there is no
`python-dotenv` dependency.

**`proxies.txt`** — one exit per line, `#` for comments:

```
# host:port, credentials come from .env
198.51.100.10:12345
198.51.100.11:12345
```

Leave it out entirely and the scrapers run direct, from the server's own
IP. That is usually enough to get the IP rate-limited, so for anything
beyond a trial run, populate it.

Check the list before relying on it:

```powershell
python check_proxies.py --file proxies.txt
```

`data\` is created on first run and holds `results.db`, `settings.json` and
the per-platform Chrome profiles. **Back up `data\` and nothing else** —
everything else is reproducible from git.

---

## 5. First run, by hand

Do this before setting up any service. It is the only way to see the real
errors.

> Do **section 7 (logins) first** if you want a scrape to return anything.
> Every platform needs a signed-in profile, and Perplexity returns nothing
> at all without one. The steps here will start the UI either way, which is
> enough to prove the install.

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Open `http://localhost:8501` **on the server itself** first. Once the
logins in section 7 are done, paste one prompt, pick Gemini (headless, so
it works in any session), and run it. A row with a response back means
the browser stack works.

Then open the firewall and try from another machine:

```powershell
New-NetFirewallRule -DisplayName "LLM Visibility Tracker" `
  -Direction Inbound -Protocol TCP -LocalPort 8501 -Action Allow
```

`--server.address 0.0.0.0` is what makes it listen on anything but
localhost. Without it the firewall rule changes nothing.

> Streamlit has no authentication. Port 8501 open to the internet means
> anyone can run scrapes on your proxies. Keep the rule scoped to your
> office range (`-RemoteAddress`), or put it behind a reverse proxy that
> does auth.

---

## 6. Keep it running

Two options. Use the Scheduled Task unless you have a reason not to — it
needs nothing installed and survives reboots.

### Scheduled Task (recommended)

`run_tracker.bat` is in the repo. Register it to start at boot:

```powershell
$action  = New-ScheduledTaskAction -Execute `
  "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker\run_tracker.bat"
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -RestartCount 3 `
             -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "LLM Visibility Tracker" `
  -Action $action -Trigger $trigger -Settings $set `
  -RunLevel Highest
# NOTE: no -User/-Password, deliberately. The task then runs in the
# logged-on interactive session, which is what the headed ChatGPT and
# Google scrapers need -- see section 8. Pair it with auto-logon.
```

`-ExecutionTimeLimit ([TimeSpan]::Zero)` means "no time limit". Without it
Windows kills the task after three days, which looks exactly like a crash.

Start it now without rebooting:

```powershell
Start-ScheduledTask -TaskName "LLM Visibility Tracker"
Get-ScheduledTask   -TaskName "LLM Visibility Tracker" | Get-ScheduledTaskInfo
```

Logs land in `logs\streamlit.log`.

### NSSM (if you want a real Windows service)

```powershell
nssm install LLMTracker `
  "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker\.venv\Scripts\streamlit.exe" `
  "run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true"
nssm set LLMTracker AppDirectory "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker"
nssm set LLMTracker AppStdout    "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker\logs\streamlit.log"
nssm set LLMTracker AppStderr    "C:\Users\Administrator\Desktop\LLM-Visibility-Tracker\logs\streamlit.log"
nssm start LLMTracker
```

Set the service to run as a **named user that has logged in at least
once**, not `LocalSystem`. Chrome under `LocalSystem` runs in Session 0 with
no user profile and fails in ways that are hard to read.

---

## 7. Logins — do this before anything is scheduled

**Perplexity, ChatGPT and Gemini all need a signed-in profile.** Perplexity
gates anonymous search outright: logged out it answers "Sign up and repeat
your request" and returns no citations at all.

Logging in is **interactive** — a real browser window opens and you type
into it. It cannot be automated, and it cannot be done from a background
service. Connect over RDP and run, once per platform:

```powershell
.\.venv\Scripts\Activate.ps1
python -m scrapers.login perplexity
python -m scrapers.login chatgpt
python -m scrapers.login gemini
```

Each opens a window, waits for you to sign in, confirms the session, then
closes itself. Cookies land in `data\profiles\<platform>\` and every later
run reuses them.

Two things to know:

- **Perplexity logs in through Camoufox (Firefox), not Chrome.** Its
  scraper uses Scrapling's StealthyFetcher to clear Cloudflare, and a
  Chromium profile's cookies are unreadable to it. That is why
  `scrapling install` is in the install step — it fetches Camoufox.
- **Sessions expire.** When a scraper reports a sign-up wall, re-run the
  login for that platform. Nothing recovers this on its own.

Use accounts you are willing to lose. Automating a signed-in account
breaches these platforms' terms.

---

## 8. Headless — and why this box needs a desktop session

This is the part that most often turns a working laptop setup into a
server that returns empty rows.

**Three scrapers are hardcoded to run headed** and ignore any headless
setting:

| Scraper | Mode | Reason, from the code |
|---|---|---|
| `chatgpt.py` | **headed** | "Turnstile blocks headless regardless of URL" |
| `google.py` | **headed** | "AIO + bot-detection: always headed" |
| `google_paa.py` | **headed** | "SERP bot-detection: always headed" |
| `gemini.py` | headless OK | |
| `perplexity.py` | headless OK | via Camoufox |

A headed browser needs a real desktop to draw on. Windows services and
boot-triggered Scheduled Tasks run in **Session 0**, which has none — so
ChatGPT and Google scrapes fail there while Gemini and Perplexity work.
That asymmetry is the symptom to recognise.

So the machine must have a logged-in interactive session whenever those
platforms are scraped:

1. Enable automatic logon for the account that runs the app
   (`netplwiz`, or the `AutoAdminLogon` registry values).
2. Register the task with **"Run only when user is logged on"** — not
   "whether or not" — so it lands in that session. In the section below,
   that means dropping `-User`/`-Password` and leaving the task to run
   interactively.
3. Leave the console session logged in. Disconnecting RDP with the X
   keeps it alive; **signing out kills it** and every headed scrape with
   it.

If you must disconnect cleanly, redirect the session back to the console
first so it keeps a desktop:

```powershell
tscon 1 /dest:console      # session id from: query session
```

If Gemini and Perplexity return rows but ChatGPT and Google come back
empty, this section is the cause — every time.

### Other things that break on a server

- **`channel="chrome"` with no Chrome installed.** The fallback to bundled
  Chromium works but has a different fingerprint, and the defended
  platforms notice. Install real Chrome.
- **A service running as `LocalSystem`.** No writable user profile, so the
  persistent context cannot be created. Use a named account.
- **The profile folder going stale.** `data\profiles\<platform>` carries
  cookies and fingerprint state. If one platform starts failing while the
  others are fine, delete that folder and re-run its login.
- **A Chrome update mid-run.** Chrome updates itself; Playwright does not.
  After a major Chrome bump, re-run `python -m playwright install chromium`.

---

## 9. Updating

```powershell
cd C:\Users\Administrator\Desktop\LLM-Visibility-Tracker
Stop-ScheduledTask -TaskName "LLM Visibility Tracker"
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Start-ScheduledTask -TaskName "LLM Visibility Tracker"
```

`data\` is untouched by a pull, so results and settings survive.

---

## 10. When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| Page loads on the server, not from outside | Listening on localhost only | `--server.address 0.0.0.0`, and open the firewall |
| `Activate.ps1 cannot be loaded` | Execution policy | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| Every scrape returns empty | No proxies, IP rate-limited | Fill `proxies.txt`, verify with `check_proxies.py` |
| One platform empty, others fine | Stale profile | Delete `data\profiles\<platform>` |
| Gemini rows show `citation_count = 0` | Expected | Anonymous Gemini does not expose source URLs as anchors — see README |
| Task shows "Running" but nothing serves | Streamlit died, task did not | Check `logs\streamlit.log`; `-RestartCount` only covers a process exit |
| `playwright._impl._errors.Error: Executable doesn't exist` | Browser binaries missing | `python -m playwright install chromium` inside the venv |
| ChatGPT + Google empty, Gemini + Perplexity fine | Headed scrapers have no desktop (Session 0) | Auto-logon, task "only when user is logged on", keep the session alive -- section 8 |
| Perplexity returns "Sign up and repeat your request" | Not logged in, or session expired | `python -m scrapers.login perplexity` over RDP |
| Perplexity fails but ChatGPT works | Camoufox missing | `scrapling install` inside the venv |

To see real errors, stop the task and run `streamlit run app.py` by hand in
a console. The log file catches stdout but not every browser-level failure.

---

## 11. Before you call it done

- [ ] `python --version` reports 3.11 or 3.12
- [ ] `where.exe chrome` finds a machine-wide install
- [ ] `.env` present, and **not** tracked by git (`git status` should ignore it)
- [ ] `proxies.txt` populated, `check_proxies.py` passes
- [ ] `python -m scrapers.login` run for perplexity, chatgpt AND gemini
- [ ] Auto-logon enabled, and the task set to "run only when user is logged on"
- [ ] One prompt scraped end to end on EACH platform, by hand, with cited URLs returned
- [ ] Firewall rule scoped to your network, not `Any`
- [ ] Task survives `Restart-Computer`
- [ ] `data\` included in whatever backs this machine up
