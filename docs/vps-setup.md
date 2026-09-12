# Running this on a VPS

The tablet is the wrong place for the engine — Android's Doze mode kills
background processes, and a strategy defined by a two-hour daily window cannot
afford to be asleep at 15:40. A small always-on machine solves that, and it
solves the data problem at the same time: the Dukascopy download is 45 minutes
and a couple of gigabytes, which is miserable on a tablet and trivial on a VPS.

## What to buy

The workload is small. Python with no heavy dependencies, a few hundred
thousand candles in memory, one core doing bar-by-bar arithmetic.

| | Minimum | Comfortable |
|---|---|---|
| vCPU | 1 | 2 |
| RAM | 1 GB | 2 GB |
| Disk | 20 GB | 40 GB |
| OS | Debian 12 or Ubuntu 24.04 | same |

Disk is the one that bites: three years of gold ticks is 1–2 GB of cache, plus
the CSVs on top. 20 GB is fine, 10 GB is not.

Any provider at the entry tier works — **Hetzner** (CX22, ~4 €/month) is the
best value in Europe; **Scaleway** and **OVH** are French if you prefer that.
Pick a datacentre near you: it makes no difference to the download, only to
how responsive SSH feels from the tablet.

Skip the managed extras. You want a plain VPS with root SSH.

## Connecting from a tablet

Either works:

- **Termux** (from F-Droid, not the Play Store — the Play version is
  abandoned): `pkg install openssh`, then `ssh root@your-ip`.
- **JuiceSSH** or **Termius**: friendlier for a touchscreen, with saved
  connections and an on-screen key row that actually has ctrl and tab.

Use an SSH key rather than a password. Your provider's panel will let you paste
a public key when creating the machine; generate one in Termux with
`ssh-keygen -t ed25519`.

## Setting it up

One command on a fresh machine:

```bash
curl -fsSL https://raw.githubusercontent.com/xilowyz-jpg/Ii/claude/salut-6jamph/scripts/setup-vps.sh | bash
```

It installs Python, git and tmux, clones the repo, builds a virtualenv, and
runs the test suite. It is idempotent — run it again after a failure or a
reboot and it picks up where it left off. If the tests do not pass it stops
rather than leaving you with an install you might trust.

Piping a script from the internet into bash is a habit worth being careful
with. If you would rather read it first:

```bash
git clone -b claude/salut-6jamph https://github.com/xilowyz-jpg/Ii.git fxagents
less fxagents/scripts/setup-vps.sh
bash fxagents/scripts/setup-vps.sh
```

## Use tmux for anything long

This is the part people learn the hard way. Close the SSH session — or let the
tablet sleep, or lose Wi-Fi — and every process you started dies with it. A
45-minute download will not survive a tablet screen timeout.

```bash
tmux new -s fetch          # start a session that outlives the connection

fxagents fetch --instruments XAU_USD --granularity M5 \
    --from 2022-01-01 --to 2024-12-31

# ctrl-b then d            to detach and leave it running
# tmux attach -t fetch     to come back, from any device
```

You can close the tablet, go do something else, and reconnect an hour later to
find it finished.

## First run

Do a single month before committing to three years:

```bash
cd ~/fxagents && source .venv/bin/activate

fxagents fetch --instruments XAU_USD --granularity M5 \
    --from 2024-05-01 --to 2024-05-31

head -3 data/XAU_USD_M5.csv
```

Gold should read around 2300–2400 for May 2024. If it comes out near 2.35 the
point value is wrong — the fetcher checks this and refuses to write, but see
the numbers yourself anyway.

Then the long one, and the scan:

```bash
fxagents smc-scan --instruments XAU_USD --granularity M5 \
    --source csv --bars 200000 --near-misses 4
```

Remember the warmup: the daily MA200 needs ~57,600 M5 bars before the strategy
can trade at all, so fetch at least two years or the scan will find nothing for
reasons unrelated to your rule.

## Basic hardening

The machine is on the public internet. Two things, five minutes:

```bash
# 1. Disable password logins (after confirming your key works)
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# 2. Firewall: SSH only, for now
sudo apt-get install -y ufw
sudo ufw allow OpenSSH
sudo ufw --force enable
```

Do not open any other port yet. When there is an API to serve, it goes behind
HTTPS with a real certificate and its own authentication — not a port opened
"just to test".

And when that time comes: **no broker API keys on this machine** while the
system is only scanning and alerting. A VPS that cannot place an order cannot
place a wrong one.

## What comes next

Once the scan produces setups you have checked against your own chart, the
remaining work is:

1. a scheduled scan (systemd timer, every 5 minutes during the session window);
2. an alert when five stars align — Telegram is the shortest path to a
   notification on an Android tablet, a bot token and one HTTP call;
3. a small web dashboard the tablet opens, showing recent setups and the
   current state.

None of it is worth building until the detector agrees with you about what an
order block is.
