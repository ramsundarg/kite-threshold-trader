# Threshold trader for Zerodha Kite

A small bot built on the official Zerodha library [pykiteconnect](https://github.com/zerodha/pykiteconnect).
It contains two strategies:

- **Threshold trader** (`trader.py`, settings in `config.json`), described below.
- **Covered calls** (`cc_trader.py`, settings in `covered_call.json`). See [section 4](#4-covered-call-strategy-cc_traderpy).

For each stock in `config.json`, the threshold trader applies these rules:

| Situation | Action |
|---|---|
| Not holding, price **≤ `buy_below`** | **BUY** `quantity` shares |
| Holding, price **≥ `sell_above`** | **SELL** everything (take the signal exit) |
| Holding, loss **≥ 5 %** from your average price | **SELL** everything (stop-loss) |
| Holding, gain **≥ 15 %** from your average price | **SELL** everything (take-profit) |

The stop-loss and take-profit checks run before the buy/sell signal. The bot never
buys more of a stock it already holds, and it never places a second order while the
first is still unfilled. Every order is a LIMIT order 0.5 % through the current
price, so it fills like a market order but cannot fill at a runaway price.

You can run it in three places. Pick one:

1. **[On your Windows PC](#1-run-it-on-your-windows-pc)**: the PC must stay on during market hours.
2. **[On Zerodha's servers (GTT orders)](#2-let-zerodha-run-it-gtt-orders)**: no PC needed, but only for delivery (CNC) trades.
3. **[On a cloud server](#3-run-it-in-the-cloud)**: always on, ~₹400–800/month, and gives you the static IP that Zerodha requires for API orders.

> ⚠️ **Real money.** Run `--demo` first, then paper mode (the default) for a few
> days. Use `--live` only after that. This is sample code, not investment advice.

---

## 0. One-time: get a Kite Connect API key

1. Go to **https://developers.kite.trade** and sign up. This developer account is separate from your
   normal Kite login. Link it to your Zerodha client ID.
2. Choose a plan. The live bot (`trader.py`) reads live prices, so it needs a plan that
   **includes market data**. Zerodha's free "Personal" plan can place orders but has no live quotes.
   It still works with the GTT option (step 2) if you pass `--last-price`.
   Check the current plans and prices on the developer site.
3. **Create new app**:
   - *App name*: anything, e.g. `threshold-bot`
   - *Zerodha Client ID*: your client ID (e.g. `AB1234`)
   - *Redirect URL*: `http://127.0.0.1/`
   - *Postback URL*: leave empty
4. Open the app page and copy the **API key** and **API secret**. Treat the secret like a password.
5. **Static IP.** Under SEBI's retail-algo rules, Zerodha only accepts API *orders* from an IP
   address you have registered in the app settings. Home internet usually has a changing IP, so
   check your app page for the "Static IP" field. See [section 3](#3-run-it-in-the-cloud) for the cloud fix.
   Reading prices and running `--demo` or paper mode need no static IP.

---

## 1. Run it on your Windows PC

### Install (once)

1. Install **Python 3.12 or newer** from https://www.python.org/downloads/windows/.
   On the first installer screen, tick **"Add python.exe to PATH"**.
2. Copy this folder to your PC, e.g. `C:\trading\kite_threshold_trader`.
3. Double-click **`setup.bat`**. It creates a private Python environment, installs the
   libraries, copies `config.example.json` to `config.json`, and runs the self-tests.
   It should end with `Setup OK`.

### Add your API key (once)

Open **Command Prompt** (Start → type `cmd`) and run the two commands below, using your own values:

```bat
setx KITE_API_KEY "your_api_key_here"
setx KITE_API_SECRET "your_api_secret_here"
```

Close that window. The keys only apply to windows you open *after* this. They are stored
in your Windows user profile, not in the project files, so you can share the folder safely.
To change a key later, run `setx` again.

### Pick your stocks

Edit **`config.json`** in Notepad:

```json
{
  "product": "MIS",            // MIS = intraday (auto square-off at 15:15), CNC = delivery
  "poll_seconds": 5,           // check prices every 5 s
  "entry_cutoff": "15:00",     // no NEW buys after this time (exits still happen)
  "squareoff_time": "15:15",   // MIS only: close everything at this time
  "rules": [
    { "symbol": "NSE:INFY", "buy_below": 1400, "sell_above": 1550, "quantity": 5,
      "stop_loss_pct": 5, "take_profit_pct": 15 }
  ]
}
```

(JSON does not allow `//` comments. They are shown here only to explain the fields, so leave them out of the real file.)

- `symbol` is `EXCHANGE:TRADINGSYMBOL`, exactly as Kite shows it (`NSE:RELIANCE`, `BSE:500325`).
- `stop_loss_pct` / `take_profit_pct` default to 5 and 15 if left out.
- `allow_short: true` (MIS only) also *sells short* when the price is above `sell_above` and you hold nothing.

### Try it

1. **`run_demo.bat`**: fake random prices, no Zerodha connection at all. It shows what the bot
   would do and the resulting profit or loss.
2. **Every trading morning, after 06:00 IST**, double-click **`login.bat`**. Zerodha tokens expire
   daily and logging in needs your password and TOTP, so this step cannot be automated.
   - A browser opens the Kite login page. Log in.
   - The browser then goes to `http://127.0.0.1/?request_token=XXXX&...` and shows
     "can't connect". **That's expected.** Copy the `XXXX` part from the address bar
     and paste it into the window.
3. **`run_paper.bat`**: live prices, *simulated* orders (nothing is sent to Zerodha).
   Output goes to `trader.log`. Open it in Notepad to see what the bot did.
4. When you are happy with it, run **`run_live.bat`** to place real orders.

The bot waits until 09:15 and stops by itself: at 15:15 for MIS (after squaring off),
at 15:30 for CNC. It does not know about exchange holidays. On a holiday it just finds no trades.

### Start it automatically each morning (optional)

Task Scheduler → **Create Basic Task** → *Daily*, 09:05 → *Start a program* →
browse to `run_live.bat` (or `run_paper.bat`). Then open the task's properties and:
- tick **Run whether user is logged on or not**, and
- on *Conditions*, tick **Wake the computer to run this task**.

In Settings → Power, also set *Sleep* to **Never** while plugged in. A sleeping PC can't trade.
You still need to run `login.bat` yourself before 09:05 each day.

---

## 2. Let Zerodha run it (GTT orders)

Zerodha has no way to run your Python code. It does run **GTT (Good Till Triggered)**
orders on its own servers for up to a year, even when your PC is off. GTTs cover the
same rules, for **delivery (CNC) only**:

```bat
login.bat
:: 1) buy 5 INFY when the price falls to 1400
.venv\Scripts\python gtt_setup.py entry NSE:INFY --buy-below 1400 --qty 5

:: 2) after the buy has filled and shows in Holdings (next day):
::    one order that sells at -5% (stop-loss) OR at min(+15%, 1550), whichever comes first
.venv\Scripts\python gtt_setup.py protect NSE:INFY --sell-above 1550

.venv\Scripts\python gtt_setup.py list
.venv\Scripts\python gtt_setup.py delete 123456
```

If your API plan has no market data, add `--last-price 1450` with today's price.

Limitations:
- The entry and the protection are two separate GTTs, so you run `protect` after each buy.
- GTTs are CNC only. There is no intraday (MIS) version, and no automatic re-buy after an exit.

You can also set these up by hand, with no code at all: in the Kite app, open the stock → **Create GTT** →
*Single* (for the buy) or *OCO* (stop-loss plus target on a holding).

---

## 3. Run it in the cloud

A small Linux server in **Mumbai** stays on all day and has a **fixed (static) IP**. That is
what Zerodha needs for API orders. These steps use AWS Lightsail, which is the simplest option.
DigitalOcean (Bangalore), Azure or Google Cloud (Mumbai) work the same way.

1. **Create the server.** At https://lightsail.aws.amazon.com: *Create instance* → Region
   **Mumbai (ap-south-1)** → Linux → **Ubuntu 24.04** → the smallest plan is enough.
2. **Networking tab → Create static IP** and attach it to the instance. Copy that IP into
   your Kite app's **Static IP** field (section 0, step 5).
3. **Connect.** Click *Connect using SSH* in the browser, or from Windows PowerShell run
   `ssh -i LightsailKey.pem ubuntu@<static-ip>`.
4. **Install:**
   ```bash
   sudo timedatectl set-timezone Asia/Kolkata
   sudo apt update && sudo apt install -y python3-venv tmux
   # copy the folder up from Windows PowerShell:
   #   scp -i LightsailKey.pem -r C:\trading\kite_threshold_trader ubuntu@<static-ip>:~
   cd ~/kite_threshold_trader
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cp -n config.example.json config.json   # then edit with: nano config.json
   .venv/bin/python -m unittest test_strategy
   ```
5. **API keys**, kept in a file only you can read:
   ```bash
   printf 'export KITE_API_KEY=your_key\nexport KITE_API_SECRET=your_secret\n' > ~/.kite_env
   chmod 600 ~/.kite_env
   ```
6. **Schedule it.** Run `crontab -e` and add this line (weekdays 09:05 IST):
   ```
   5 9 * * 1-5 cd ~/kite_threshold_trader && . ~/.kite_env && .venv/bin/python trader.py --live >> trader.log 2>&1
   ```
   (Leave out `--live` for paper mode.)
7. **Every morning** between 06:00 and 09:05, log in once:
   ```bash
   ssh ... ; cd ~/kite_threshold_trader && . ~/.kite_env && .venv/bin/python login.py
   ```
   Open the printed link on your phone or PC, log in, and copy the `request_token` from the
   `127.0.0.1` address it redirects to. Paste it into the SSH window. If you forget, the 09:05
   run stops with "Access token is from a previous day" and places no orders.
8. **Watch it:** `tail -f ~/kite_threshold_trader/trader.log`

---

## 4. Covered-call strategy (`cc_trader.py`)

A **covered call** means you hold shares of a stock and sell (write) a call option on
them each month. You keep the premium as income. In exchange, you give up gains above
the strike: if the stock ends above the strike at expiry, your shares are sold at that price.

### What the bot does, per stock in `covered_call.json`

| Situation | Action |
|---|---|
| You hold ≥ 1 lot of shares and no call is written | **Sell** the call with the nearest expiry that is ≥ `min_days_to_expiry` days away, at the lowest strike ≥ spot + `otm_pct` % |
| The call has lost `take_profit_pct` % of its value (e.g. sold at ₹20, now ₹4) | **Buy it back** and write a fresh one on the next cycle |
| ≤ `roll_days_before_expiry` days to expiry | **Buy it back** (roll). The next cycle writes the next month's call |
| The stock is down `stock_stop_loss_pct` % from your average price | **Buy back the call first, then sell the covered shares**, then stop managing that stock |
| You hold fewer shares than the calls you have written | **Buy back** the uncovered part, so the bot is never naked short |

Orders are LIMIT orders at the best bid (when selling) or best offer (when buying).
Any order not filled within `order_timeout_seconds` is cancelled and re-decided at the
new price. After a rejection (usually margin), the bot waits 15 minutes before trying again.
The premium you actually received is recorded in `.cc_premiums.json`, which the take-profit rule uses.

### Before you start: requirements

- **Only F&O stocks**, and only in **whole lots**. Lot sizes are several hundred shares
  (check the lot size on NSE or in Kite's option chain), so one lot of a ₹1,500 stock is ~₹5–6 lakh
  of shares. With fewer than one lot, the bot does nothing.
- The shares must be in your **Holdings** (bought as CNC). The bot never buys shares for this strategy.
- **Margin.** Selling a call needs F&O margin even though you hold the shares. Pledge the shares
  (Kite → Holdings → the stock → *Pledge*) to use them as collateral. SEBI also requires part of the
  option-selling margin to be in cash, so keep some cash in the account. Kite shows the margin
  needed when you place an order.
- **F&O must be enabled** on your Zerodha account (Console → Segments).
- **Physical settlement.** Stock options in India settle by delivering shares. If a written call is
  in the money at expiry, your shares are handed over at the strike. That is the intended
  covered-call outcome, but rolling 5 days before expiry (the default) usually avoids it. Zerodha
  also asks for higher margin in expiry week.

### Settings (`covered_call.json`)

```json
{ "underlying": "NSE:INFY", "lots": 1, "otm_pct": 5.0,
  "min_days_to_expiry": 20, "max_days_to_expiry": 60, "roll_days_before_expiry": 5,
  "take_profit_pct": 80.0, "stock_stop_loss_pct": 5.0 }
```

`"stock_stop_loss_pct": null` turns the stock stop off. With the stop off, the bot just keeps
writing calls against shares you plan to hold anyway, which is the classic covered call.

### Run it

1. **`run_cc_demo.bat`** simulates one year with made-up prices and option prices from a
   Black-Scholes model. It prints every order and compares the result with simply holding
   the shares. Try other price paths with
   `.venv\Scripts\python cc_trader.py --demo --seed 3`.
   On these simulated paths, a 5 % stock stop usually triggers within a few weeks, so choose it deliberately.
2. `login.bat`, then **`run_cc_paper.bat`**: live data. It only *logs* the orders it would send,
   in `cc_trader.log`. Nothing reaches Zerodha. In paper mode it logs the same intended
   order again every minute. That is expected.
3. **`run_cc_live.bat`**: real orders. On the cloud server, use this cron line:
   `20 9 * * 1-5 cd ~/kite_threshold_trader && . ~/.kite_env && .venv/bin/python cc_trader.py --live >> cc_trader.log 2>&1`

One run of `cc_trader.py` a day is enough. Options expire on a monthly cycle, and the bot
checks every 60 s during market hours.

---

## Files

| File | What it does |
|---|---|
| `strategy.py` | The buy/sell/stop-loss/take-profit rules. Pure logic, no Zerodha calls. |
| `broker.py` | Zerodha adapter (real orders), paper broker (fake fills on live prices), demo broker (fake prices). |
| `trader.py` | Main loop: market hours, pending-order guard, MIS square-off. |
| `login.py` | Daily login, saves the token to `.kite_session.json`. |
| `gtt_setup.py` | Places the same rules as server-side GTT orders. |
| `covered_call.py` | The covered-call rules (write / roll / take-profit / stop-loss). Pure logic. |
| `cc_trader.py` | Covered-call runner: live Kite, paper, and an offline Black-Scholes demo. |
| `test_strategy.py`, `test_covered_call.py` | Self-tests (`python -m unittest test_strategy test_covered_call`). |
| `*.bat` | Windows double-click launchers. |

Never share `.kite_session.json` or your API secret. Anyone who has them can trade your account.
