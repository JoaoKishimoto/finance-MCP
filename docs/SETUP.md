# Setup, step by step

Order: **Pluggy → brapi → GitHub OAuth app → DNS → VPS (Docker, firewall) → .env → start → connect claude.ai → first chat.**

New to Docker? Read [DOCKER.md](DOCKER.md) first (about 10 minutes).

---

## Step 1: Pluggy credentials and Item IDs

1. Create an account at **dashboard.pluggy.ai**, create a **Team**, then an **Application**.
2. In the application page, under **Credenciais**, copy the **Client ID** and **Client Secret**. Keep them in your password manager. Never paste the secret into a chat.
3. At **meu.pluggy.ai**, sign up and connect every institution you use: checking account, credit cards, and your broker for stocks and Tesouro. ⚠️ **Do this while your Pluggy trial is active.** After it ends, existing connections keep working but you can't add new ones.
4. Make sure the **MeuPluggy** connector is on: **Customization → Connectors**. If it says "Default to all selected", it's already on. Otherwise search for MeuPluggy and switch it on.
5. Link Meu Pluggy to **your application** to get the Item IDs. Two ways:
   - **Dashboard:** Applications → your app → **Demo** → **Connect Account** → choose **MeuPluggy** → log in. Then click each entry under **Connected Items** to copy its `itemId`.
   - **Script** (if you can't find the Demo page): on your computer, in the project folder:
     ```bash
     cp .env.example .env    # then put PLUGGY_CLIENT_ID / PLUGGY_CLIENT_SECRET in it
     python3 scripts/connect.py
     ```
     A browser tab opens. Choose **MeuPluggy**, log in, pick a bank. The Item ID is printed. Repeat once per bank.

   You get one Item per institution. Save all the IDs.

## Step 2: brapi token (stock prices)

Sign up at **brapi.dev** and copy the free token. Without it, only a few test tickers work.

## Step 3: GitHub OAuth app (your login for claude.ai)

1. Go to **github.com/settings/developers → OAuth Apps → New OAuth App**.
2. Fill in:
   - **Application name:** `Finance MCP`
   - **Homepage URL:** `https://finance.joaokishimoto.dev`
   - **Authorization callback URL:** `https://finance.joaokishimoto.dev/auth/callback`
3. Click **Register**, then copy the **Client ID**. Click **Generate a new client secret** and copy the secret too.

The server only asks GitHub for `read:user`, which is your public profile, and only lets in the usernames listed in `ALLOWED_GITHUB_USERS`.

## Step 4: DNS

Where your domain `joaokishimoto.dev` is managed (registrar or Cloudflare), add:

| Type | Name | Value |
|---|---|---|
| A | `finance` | your VPS public IPv4 (Oracle console → Instance → Public IP) |

If the domain is on Cloudflare, set the record to **DNS only** (grey cloud) so Caddy can get its own certificate.

Check it from your computer: `dig +short finance.joaokishimoto.dev` should print the VPS IP. Propagation can take a few minutes.

## Step 5: Prepare the VPS (Ubuntu 26.04, ARM)

SSH in. Oracle's default user is `ubuntu`:

```bash
ssh ubuntu@YOUR_VPS_IP
```

### 5.1 Updates and Docker

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sudo sh        # official Docker install script (detects ARM)
sudo usermod -aG docker $USER                       # run docker without sudo
exit                                                # log out and back in for the group to apply
```

Then log back in and check:

```bash
docker run --rm hello-world
docker compose version
```

If the script doesn't support 26.04 yet, use Ubuntu's own packages instead: `sudo apt install -y docker.io docker-compose-v2`.

### 5.2 Open ports 80 and 443. Oracle has TWO firewalls.

**(a) Oracle Cloud console.** Go to **Networking → Virtual Cloud Networks →** your VCN **→** your subnet **→ Security List → Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |
| `0.0.0.0/0` | UDP | `443` (optional, HTTP/3) |

**(b) Ubuntu's iptables.** Oracle images ship with a REJECT rule. Insert the ACCEPT rules **before** it:

```bash
sudo iptables -L INPUT --line-numbers      # note the line number N of the "REJECT" rule
sudo iptables -I INPUT N -p tcp -m state --state NEW --dport 80  -j ACCEPT
sudo iptables -I INPUT N -p tcp -m state --state NEW --dport 443 -j ACCEPT
sudo iptables -I INPUT N -p udp --dport 443 -j ACCEPT
sudo netfilter-persistent save             # keep the rules after reboot
```

Replace `N` with that number. **Don't** run `ufw enable` on top of this. Mixing ufw with Oracle's rules and Docker's rules gets confusing quickly.

### 5.3 Folders

```bash
sudo mkdir -p /opt/proxy /opt/finance
sudo chown $USER:$USER /opt/proxy /opt/finance
```

## Step 6: Copy the project to the VPS

From **your computer**, in `~/.dev/Projetos/MCPs/Finance`:

```bash
rsync -av --exclude .git --exclude .env --exclude 'data/' --exclude '__pycache__' ./ ubuntu@YOUR_VPS_IP:/opt/finance/
rsync -av proxy/ ubuntu@YOUR_VPS_IP:/opt/proxy/
```

`rsync` only sends what changed. Run the same command again later to deploy updates.

## Step 7: Create the .env files (on the VPS)

### 7.1 Proxy

```bash
cd /opt/proxy
cp .env.example .env
nano .env            # ACME_EMAIL=your email (for Let's Encrypt notices)
```

### 7.2 Finance MCP

```bash
cd /opt/finance
cp .env.example .env
openssl rand -hex 32         # copy the output; it will be JWT_SIGNING_KEY
nano .env
chmod 600 .env               # only your user can read it
```

Fill in each line:

| Variable | Where it comes from |
|---|---|
| `PLUGGY_CLIENT_ID` / `PLUGGY_CLIENT_SECRET` | Step 1.2 |
| `PLUGGY_ITEM_IDS` | Step 1.5, comma-separated, no spaces: `id1,id2,id3` |
| `BRAPI_TOKEN` | Step 2 |
| `PUBLIC_BASE_URL` | `https://finance.joaokishimoto.dev` (already filled in) |
| `AUTH_MODE` | `github` (already filled in) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | Step 3 |
| `ALLOWED_GITHUB_USERS` | Your GitHub username |
| `JWT_SIGNING_KEY` | The `openssl rand -hex 32` output |

Rules for `.env`: one `KEY=value` per line, **no quotes, no spaces around `=`**, and lines starting with `#` are comments.

In nano, save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

## Step 8: Start everything

```bash
# 1) Proxy first: it creates the shared "proxy" network
cd /opt/proxy && docker compose up -d

# 2) The MCP
cd /opt/finance && docker compose up -d --build

# 3) Check
docker compose ps                                    # finance-mcp should become "healthy"
docker compose exec finance-mcp python -m finance_mcp.check
docker compose -f /opt/proxy/docker-compose.yml logs caddy | tail -20   # look for "certificate obtained"
```

From your computer:

```bash
curl https://finance.joaokishimoto.dev/health                            # → ok
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://finance.joaokishimoto.dev/mcp   # → 401 (login required: good!)
```

## Step 9: Connect claude.ai

1. claude.ai → **Settings → Connectors → Add custom connector**.
2. **Name:** `Finance`. **URL:** `https://finance.joaokishimoto.dev/mcp`.
3. Leave **Advanced settings** empty. The server registers Claude automatically.
4. Click **Add**, then **Connect**. You'll see the server's consent page, then GitHub's "Authorize" page. Approve both. You're sent back to Claude.
5. In a chat, open the **tools / "+" menu** and make sure **Finance** is on.

## Step 10: First conversation

Try something like:

> "Use my Finance connector. Read my config and ask me whatever is missing (income, payday, monthly investment target, emergency fund), then save it."

Then:

- "How much can I still spend until payday without touching my investment money?"
- "What are my monthly essential costs? And just subscriptions?"
- "Should I buy a R$ 2.400 phone in 10x on the card?"
- "Show my allocation and today's Tesouro IPCA+ rates."

If something is classified wrong, tell Claude (for example, "Smart Fit is essential for me"). It updates the rules with `update_config`, and every change is backed up.

---

## Operations

| Task | Command (on the VPS) |
|---|---|
| Deploy a new version | From your computer, run the rsync from Step 6. Then `cd /opt/finance && docker compose up -d --build` |
| Logs | `cd /opt/finance && docker compose logs -f --tail 100` |
| Change `.env` | `nano .env` then `docker compose up -d` |
| See your rules | `docker compose exec finance-mcp cat /data/config.yaml` |
| List config backups | `docker compose exec finance-mcp ls /data/backups` |
| Roll back a config change | `docker compose exec finance-mcp cp /data/backups/config-XXXX.yaml /data/config.yaml` |
| Back up the volume to your computer | `docker compose cp finance-mcp:/data ./finance-data-backup`, then `scp` it |
| RAM / CPU | `docker stats` |
| Clean old images | `docker image prune` |

**Open Finance consents expire**, usually after 12 months. When `get_connections` shows an error or an old sync date, reconnect from your computer with `python3 scripts/connect.py --update ITEM_ID`.

**Oracle Always Free:** Oracle may reclaim Always Free instances it considers idle for a long time. If yours is Always Free, keep an eye on the account emails.

## Local testing (optional, on your computer, without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate.fish                     # bash/zsh: source .venv/bin/activate
pip install -e ".[dev]"
pytest                                             # unit tests
python -m finance_mcp.check                        # reads ./.env automatically
env AUTH_MODE=none HOST=127.0.0.1 DATA_DIR=./data python -m finance_mcp
# in another terminal:
npx @modelcontextprotocol/inspector                # UI to call tools; URL: http://127.0.0.1:8000/mcp
```

`AUTH_MODE=none` refuses to start unless `HOST` is `127.0.0.1`, so the server can never run open on the internet by mistake.
