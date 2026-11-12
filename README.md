# Day-ahead TB spreads – Austria and Germany-Luxembourg

A free, self-updating website showing daily, weekly, monthly and yearly TB1, TB2 and TB4
day-ahead price spreads for the AT and DE-LU bidding zones.

**How it works:** every day GitHub runs `scripts/update_tb.py`. The script fetches the
latest day-ahead prices from the ENTSO-E Transparency Platform, calculates the spreads and
saves them in `docs/data/tb_daily.json`. GitHub Pages then publishes `docs/index.html`,
which reads that file. You don't need a server, and it costs nothing.

The data is already filled in from 1 Jan 2019 to 12 Nov 2026, so the site works as soon as it's published. The API key only lets it add new days.

---

## 1. Request an ENTSO-E API key (free, takes a few days)

1. Register at <https://transparency.entsoe.eu/> (top right: *Login* → *Register*).
2. Send an email to **transparency@entsoe.eu** with the subject **"Restful API access"**
   and the email address of your new account in the body.
3. Once they confirm, log in and go to *My Account Settings* → *Web API Security Token*
   → *Generate a new token*. Copy the token.

## 2. Put the files on GitHub

1. On GitHub: **New repository** → name it e.g. `tb-spreads` → **Public**
   (GitHub Pages is free for public repositories) → *Create repository*.
2. On the empty repository page click **uploading an existing file** and drag in the
   contents of this folder (`docs`, `scripts`, `README.md`) → *Commit changes*.
3. The `.github` folder is hidden on most computers, so add the workflow by hand:
   *Add file* → *Create new file* → type the name `.github/workflows/update.yml` → paste
   the contents of that file from this folder → *Commit changes*.

## 3. Switch on the website

1. Repository **Settings** → **Pages** → under *Build and deployment*, set **Source** to
   **GitHub Actions**.
2. Go to the **Actions** tab (click *I understand… enable them* if asked) → choose
   **Update TB spreads and publish site** → **Run workflow**.
3. After about a minute your site is live at
   `https://<your-github-username>.github.io/tb-spreads/`
   (the address is also shown in *Settings* → *Pages*).

Until you add the key (step 4), the site shows the stored data up to 12 Nov 2026. The workflow shows a yellow warning about the missing key, which is expected.

## 4. Add the API key so it updates itself

1. Repository **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**.
2. Name: `ENTSOE_API_KEY` · Secret: the token from step 1 → *Add secret*.
3. Run the workflow once by hand (as in 3.2) to fill the gap since 12 Nov 2026.

From then on it runs by itself twice a day, at 12:17 and 15:47 UTC, shortly after
ENTSO-E publishes the next day's prices. Each run that finds new prices commits the
updated data file and republishes the site.

To rebuild everything from scratch, run the workflow by hand and enter a start date such
as `2019-01-01` in the *since* field.

## 5. Show it on your Odoo website

In Odoo go to **Website** → **Edit**, drag an **Embed Code** block (in the *Content* or *Dynamic Content* group, depending on your Odoo version) onto the page, and
paste one of these (replace the address with yours):

Full page (chart, latest day, all tables):

```html
<iframe src="https://<your-github-username>.github.io/tb-spreads/"
        title="Day-ahead TB spreads, Austria and Germany"
        style="width:100%; height:2400px; border:0;" loading="lazy"></iframe>
```

Compact version (headline, chart and latest day only):

```html
<iframe src="https://<your-github-username>.github.io/tb-spreads/?view=compact&theme=light"
        title="Day-ahead TB spreads, Austria and Germany"
        style="width:100%; height:1000px; border:0;" loading="lazy"></iframe>
```

`theme=light` or `theme=dark` fixes the colour scheme; without it the page follows the
visitor's system setting. Adjust `height` so nothing is cut off on your page layout.

## Notes

- **TBx** = average of the x most expensive hourly prices minus the average of the x cheapest,
  in €/MWh. Multiply by x for €/MW per day (one full cycle of an x-hour battery).
- Since 1 Oct 2025 the day-ahead auction trades 15-minute products. Quarter-hours are
  averaged to hourly prices first, so the series stays comparable with earlier years.
- Delivery days follow Central European time (23 and 25-hour days at the clock changes).
- Prices are the SDAC auction (ENTSO-E classification sequence 1). The separate EXAA
  auction that ENTSO-E also publishes for AT and DE-LU is ignored.
- GitHub may pause scheduled runs after 60 days without repository activity. The daily
  data commits normally prevent that. If it ever happens, re-enable the workflow in the
  **Actions** tab.
- To run it on your own computer: `ENTSOE_API_KEY=... python scripts/update_tb.py`
  (Python 3.9+, no extra packages needed).
