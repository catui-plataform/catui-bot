# Setup

Repository: https://github.com/catui-plataform/catui-bot.

## Discord

Create or select your Discord Application in the Developer Portal. The application's public key is not the bot token and is not needed: this bot connects via the Discord Gateway, not an HTTP interaction endpoint.

1. In the Discord Developer Portal, open **Bot**, copy the token, and enable **Message Content Intent**.
2. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`.
3. Permissions: View Channels, Send Messages, Embed Links, and Read Message History.
   Do not grant Administrator, Manage Server, Manage Roles, or access to other channels.
4. Create a private text channel. Deny View Channel to `@everyone` and all other roles; grant member-level access only to yourself and the bot. Discord server administrators can still view private channels; use a dedicated server without other admins if isolation is required.
5. Enable Developer Mode in Discord and copy **your user ID** and the **server (guild) ID**.
   The application ID cannot be used as your user ID.

The bot strictly enforces owner ID, guild ID, and channel ID simultaneously. Other users, other channels, and DMs cannot use commands, buttons, or chat. If the channel is made public, the bot will refuse to operate.

## Square Cloud

The bot runs on Square Cloud. Configure its runtime variables directly in the
Square dashboard; the application reads only the hosting environment. No local
`.env` is loaded, committed or included in the deployment package.

Deployment uses the Square Cloud GitHub App already linked to this repository.
Pushes to `main` trigger Square's deployment integration. GitHub Actions checks
the code; Square handles deployment and restart. No separate Square API key,
application ID or custom deployment job is required in this repository.

Discord, GitHub and DeepSeek runtime credentials belong in the Square dashboard.
Core-build credentials belong in the Actions secrets described in [cores.md](cores.md).

Configure these variables:

| Variable | Value |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from the Developer Portal Bot tab |
| `DISCORD_OWNER_ID` | Your Discord user ID (numeric snowflake) |
| `DISCORD_GUILD_ID` | Your Discord server (guild) ID |
| `DISCORD_CHANNEL_ID` | Your private text channel ID |
| `GITHUB_TOKEN` | Fine-grained PAT scoped to `catui-bot` and `CATui`, Actions: Read and write, Contents: Read and write, Metadata: Read |
| `APPROVAL_SIGNING_KEY` | Random string with at least 32 characters; must match the Actions secret |
| `DEEPSEEK_API_KEY` | Key from your DeepSeek account; optional if starting without chat |
| `CORE_UPDATES_ENABLED` | `false` during initial verification |
| `CHECK_INTERVAL_SECONDS` | `3600` |
| `STATE_PATH` | `data/bot.sqlite3` |

Use the same `APPROVAL_SIGNING_KEY` value in the Square hosting environment
and the GitHub Actions secret. Never commit keys or paste them into chat or logs.

Release summaries use your DeepSeek API key and are cached per release description. Source repairs use small exact replacements. GitHub Actions keeps edits and incremental builds in the same runner, with separate integration/compiler and runtime-repair budgets (12 each by default, configurable with `AI_REPAIR_MAX_ATTEMPTS`, maximum 50). Python tooling exceptions receive up to four repairs (`PYTHON_REPAIR_MAX_ATTEMPTS`, maximum 10). The workflow deadline remains 240 minutes; native and CATui validation are mandatory. Failed runs preserve the staged binary, source recovery patch and actual validator logs in the diagnostic artifact.

After a code deployment, Square resumes a failed update only if its existing owner approval is still valid, the store revision is unchanged and the failed run used an older runner revision. It resubmits that exact approved plan once; completed runs from the previous dispatch are ignored while GitHub registers the new one. Cancelled updates are not resumed. The bot does not repeatedly redispatch failures against identical runner code.

Preserve the `data/` folder when updating deployments and take private SQLite backups when the application is stopped. It stores the core watchlist, proposals, approvals, and cached summaries. Do not launch a second instance sharing the same database state.

## First Verification: Without Building or Publishing

1. Start the bot and verify `Private control channel ready` in the logs.
2. Run `/cores-panel`: change the monitored cores, restart the bot, and confirm the selection persists.
3. Run `/cores-check`: inspect the status card of monitored cores. A version equal to or older than the store baseline will not generate a proposal.
4. Chat in the channel or use `/chat`. Without a DeepSeek key, commands still work and the bot notes that LLM is unconfigured. The assistant uses the economical DeepSeek model.
5. Test a command outside the private channel: access must be denied. DMs are ignored.
6. A reply saying `yes` counts only when replying directly to an update proposal message. During initial setup, `CORE_UPDATES_ENABLED=false` prevents starting builds even if approved.

After completing this test, proceed to [cores.md](cores.md) to configure and validate automated builds. Do not enable publishing based solely on the Discord test.

## References

- [Square Cloud: Discord Bots](https://help.squarecloud.app/en-us/article/how-to-host-a-discord-bot-247-e2udcn/)
- [DeepSeek: API documentation](https://api-docs.deepseek.com/)
- [GitHub: Fine-grained personal access tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
