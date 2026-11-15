# Connecting Power BI and Grafana

SQLite is the only data source. Both tools read the `.db` file (or CSV exports) and never talk to Notion. Always point them at a consistent copy of the database; see [Backups and Database Copies](backup-and-snapshots.md) if the tool runs on a different machine.

## Power BI

### Step 1 — Install the SQLite ODBC driver

Download and install the [SQLite ODBC driver](http://www.ch-werner.de/sqliteodbc/) (`sqliteodbc_w64.exe` for 64-bit Windows).

### Step 2 — Create a DSN and connect Power BI Desktop

1. Open **ODBC Data Sources (64-bit)** — use the 64-bit version (search "ODBC" in Start)
2. **System DSN** tab → **Add** → select **SQLite3 ODBC Driver**
3. Name the DSN (e.g. `NotionAnalytics`) and set the database path to your `notion_analytics.db` file
4. In Power BI Desktop → **Get Data** → **ODBC** → select your DSN → load the tables

> **Tip:** Set the **Data Category** of the `url` column to **Web URL** in Power BI to make Notion page links clickable in reports.

**Alternative — CSV export (no driver needed)**

Set `export_csv = true` under `[output]` in `config.toml`. After each sync, CSV files appear in `exports/`. In Power BI Desktop → **Get Data** → **Text/CSV**.

### Step 3 — Remote access

**Option A — Remote Desktop (RDP):** Enable Remote Desktop on your Windows machine and connect via Microsoft Remote Desktop (iOS/Android). [Tailscale](https://tailscale.com/) (free personal tier) makes the machine reachable from anywhere without port forwarding.

**Option B — Power BI Service:** Publish reports to the cloud and view them in Power BI Mobile. Requires a work or school Microsoft account — personal accounts are not supported. If you have one, sign in to Power BI Desktop → **File → Publish → Publish to Power BI**. For automatic cloud refresh without manually republishing, install the [On-Premises Data Gateway](https://powerbi.microsoft.com/en-us/gateway/) (Personal mode, free) on the same machine.

## Grafana

[Grafana](https://grafana.com/oss/grafana/) (free, open-source) is a cross-platform alternative to Power BI that connects directly to SQLite.

1. Install Grafana: [grafana.com/docs/grafana/latest/setup-grafana/installation/](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)
2. Install the SQLite plugin: **Administration → Plugins** → search `SQLite` → install
3. Add a data source: **Connections → Data sources → Add → SQLite** → set the path to your `notion_analytics.db`
4. Build dashboards with SQL queries against `_pages`, `_changes`, and `_comments` tables

Grafana runs as a service and is accessible from any browser — reachable remotely via [Tailscale](https://tailscale.com/) without port forwarding.
