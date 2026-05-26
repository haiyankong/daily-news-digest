# GitHub Actions Daily News Digest Setup

This repository can run a daily news digest in GitHub Actions, so it does not
depend on the Windows computer being powered on. The digest configuration is
loaded from GitHub Secrets or from a local ignored `DIGEST_CONFIG_JSON` file
when testing.

## What The Workflow Does

- Runs every day at 5:30 AM America/New_York.
- Collects recent RSS/Atom items from the configured sources.
- Uses Google News RSS as a fallback when a configured source does not expose a
  stable public RSS feed.
- Uses the OpenAI API by default to write a bilingual English-then-Chinese email digest.
- Can optionally use Anthropic Claude by setting `MODEL_PROVIDER=anthropic`.
- Sends the digest through Gmail SMTP using the sender and recipient stored in
  GitHub Secrets.
- Keeps a small sent-item state file during a run to reduce duplicate pushes
  when the same story appears through more than one feed.

## Required GitHub Secrets

In the GitHub repository, go to:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Add these secrets:

```text
OPENAI_API_KEY
DIGEST_CONFIG_JSON
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
DIGEST_RECIPIENT
```

Recommended values:

```text
GMAIL_ADDRESS=<your Gmail address>
DIGEST_RECIPIENT=<your recipient email address>
```

`GMAIL_APP_PASSWORD` should be a fresh Gmail App Password for the Gmail account
stored in `GMAIL_ADDRESS`. Do not use the normal Gmail login password.

`DIGEST_CONFIG_JSON` should contain the full digest configuration. The local
file named `DIGEST_CONFIG_JSON` is ignored by Git, so you can edit it locally
and paste its full contents into the GitHub Secret.

Optional Claude secret:

```text
ANTHROPIC_API_KEY
```

You only need this if you set `MODEL_PROVIDER=anthropic`.

If you use the GitHub CLI, you can set the config secret from PowerShell:

```powershell
Get-Content .\DIGEST_CONFIG_JSON -Raw | gh secret set DIGEST_CONFIG_JSON
```

Or paste the full file contents into the GitHub web UI when creating the secret.

## Optional GitHub Variables

You may add a repository variable:

```text
MODEL_PROVIDER=openai
OPENAI_MODEL=gpt-5-mini
```

If these variables are omitted, the script uses OpenAI with `gpt-5-mini`.

To test Claude instead, add:

```text
MODEL_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-haiku-4-5
ANTHROPIC_VERSION=2023-06-01
```

Optional variables:

```text
MAX_CANDIDATES_FOR_MODEL=120
MAX_EMAIL_CANDIDATES=90
DEFAULT_SECTION_CANDIDATE_CAP=8
MAX_FEED_ITEMS_PER_OUTLET=25
INCLUDE_GOOGLE_NEWS_FALLBACKS=true
MAX_OUTPUT_TOKENS=9000
OPENAI_MAX_OUTPUT_TOKENS=9000
ANTHROPIC_MAX_OUTPUT_TOKENS=9000
```

The section-level `candidate_cap` values inside `DIGEST_CONFIG_JSON` take
priority over `DEFAULT_SECTION_CANDIDATE_CAP`.

## First Test

After pushing the files to GitHub:

1. Open the repository on GitHub.
2. Go to `Actions`.
3. Select `Daily News Digest`.
4. Click `Run workflow`.

Manual runs skip the 5:30 AM time gate. Scheduled runs use the time gate so the
two UTC cron entries do not send duplicate emails across daylight-saving
changes. The default `lookback_days` value is `0`, so the workflow collects only
items dated today in America/New_York local time.

## Local Dry Run

For local testing, install dependencies, set the Gmail/model-provider environment
variables, and run:

```powershell
python -m pip install -r requirements.txt
python scripts\daily_news_digest.py --lookback-days 0 --allow-fallback
```

Without `--send`, the script writes the digest to `outputs/` but does not email
it. With `--allow-fallback`, it can still write a metadata-only digest if the
OpenAI API call is unavailable.

## Important Notes

- Some sources expose incomplete, delayed, or paywalled RSS metadata. The digest
  summarizes only what the feed metadata provides.
- Google News fallback entries may point through Google News redirect URLs when
  a source does not expose a stable public feed.
- GitHub Actions schedule times are not guaranteed to start at the exact minute;
  a small delay is normal.
- To change the delivery address later, update only the `DIGEST_RECIPIENT`
  secret.
