# Link Changer Bot

Tap **🔗 Change link** in Telegram, send a new link, and your Netlify page
updates in about 10 seconds. Everyone who taps **Continue to Website** after that
goes to the new link.

How it works: the bot puts your link inside `template.html` and uploads the
page to your Netlify site. Visitors only load the page from Netlify, so the page
stays fast even when the bot is asleep or restarting.

## Bot buttons

| Button | What it does |
|---|---|
| 🔗 Change link | Send a link and it goes live. You can send `shop.com`, `https://shop.com/?utm_source=tg`, or a message with a hyperlink. |
| 👀 Current link | Opens the live page and shows the link the button really goes to right now. |
| ↩️ Undo | Shows after each change. Puts back the page from before that change. |
| 🌐 Open page | Opens your Netlify page. |

Only the Telegram IDs in `ADMIN_IDS` can use the bot. Everyone else gets no reply.

## 1. Get your 4 settings

| Variable | Where to get it |
|---|---|
| `BOT_TOKEN` | @BotFather → /newbot (or use an existing bot) |
| `ADMIN_IDS` | Your Telegram ID. Add more IDs with commas: `111,222` |
| `NETLIFY_TOKEN` | Netlify → your avatar → **User settings → Applications → Personal access tokens → New access token** |
| `NETLIFY_SITE_ID` | Your template's address, like `your-template.netlify.app` (or the Site ID from **Site configuration → Site information**) |

## 2a. Host on Railway (simplest, always on)

1. Push this folder to a GitHub repo.
2. Railway → **New Project → Deploy from GitHub repo** → pick the repo.
3. Open the service → **Variables** → add the 4 settings.
4. Done. `railway.json` already sets the start command. No domain needed (the bot uses polling).

## 2b. Host on Render (free)

1. Push this folder to a GitHub repo.
2. Render → **New → Blueprint** → pick the repo (it reads `render.yaml`).
   Or **New → Web Service**, build `pip install -r requirements.txt`, start `python bot.py`.
3. Add the 4 settings when Render asks.
4. Done. On Render the bot switches to webhook mode by itself.

Render free sleeps after 15 minutes with no use. The first button tap after that
can take up to a minute to answer. Your page visitors are **not** affected,
because they never touch Render.

## 3. First use

1. Send `/start` to your bot.
2. Tap **🔗 Change link** and send your link.

The first change replaces the old AgedSocial page on that Netlify site with the
new "Shop or Learn More" page. After that, every change only swaps the link.

## Use your own design

Edit `template.html` however you like. Just keep these two things:

```html
<meta name="link-target" content="{{LINK}}">
<a id="go" href="{{LINK}}" ...>Continue to Website</a>
```

Also keep the small script at the bottom. It opens the link in the external
browser when the page is opened inside Telegram.

## Good to know

- **The bot replaces the whole Netlify site** each time. Keep anything the page
  needs (like images) inside `template.html`, or load it from another URL.
- If the Netlify site is **linked to a GitHub repo**, a push to that repo will
  overwrite the bot's page. Best to use a site that was made by drag-and-drop.
- If Netlify **auto publishing is locked**, the bot will tell you the change did
  not go live. Unlock it in Netlify → Deploys.
- Netlify limits how many deploys you can do per minute. If you change the link
  many times quickly, the bot will tell you to wait a bit.

## Run on your PC (for testing)

```bash
pip install -r requirements.txt
export BOT_TOKEN=... ADMIN_IDS=... NETLIFY_TOKEN=... NETLIFY_SITE_ID=...
python bot.py
```
