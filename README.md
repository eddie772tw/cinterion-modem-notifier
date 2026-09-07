# Cinterion Modem Notifier

A persistent ModemManager monitor that posts deduplicated modem, SIM, LTE,
data-bearer, incoming-SMS, call-state, and possible-missed-call updates to a
Discord webhook. It uses only the Python standard library, `mmcli`, and the
system `gdbus` signal monitor over all ModemManager objects.

The notifier performs one initial snapshot, then uses ModemManager D-Bus
signals as its primary trigger. It retains a full polling path only as a
fallback when the signal monitor exits, plus a cheap periodic reconciliation of
SMS and call object lists to protect against missed or buffered D-Bus signal
lines. A signal still causes the existing deduplicated snapshot code to run, so
presentation and privacy behavior remain centralized.

`RECONCILIATION_INTERVAL_SECONDS` controls that safety net and defaults to 5
seconds; `POLL_INTERVAL_SECONDS` remains the slower full-snapshot fallback and
defaults to 20 seconds. The SMS path index is pruned against the current modem
object list so it does not grow without bound after old messages are deleted.

Observed events are also upserted into a private SQLite history under
`$XDG_STATE_HOME/cinterion-modem-notifier/events.db` (normally
`~/.local/state/...`). The history is local-only and protected with mode 600;
SMS fields remain sensitive even though the database is not externally shared.

## Safety and privacy

The monitor does not enable the modem, register it, create a bearer, send SMS,
issue AT commands, or modify modem configuration. The webhook is disabled when
`DISCORD_WEBHOOK_URL` is blank. Incoming SMS notifications deliberately contain
the full sender number and full message body, so access to the webhook and its
Discord channel must be restricted.

The status snapshot includes SIM lock state and, when ModemManager exposes an
initial EPS bearer, whether that bearer is connected and which host interface
it has. Registration/packet attachment alone does not imply that the host has
an IP data connection.

Discord embeds use green for a registered/attached modem, red for a failed or
disconnected modem, yellow for signal quality below 30%, blue for SMS, and gray
for calls or other event types. Status deduplication buckets signal quality at
`<90`, `<75`, `<50`, and `<25`; fluctuations within one bucket do not trigger
another status notification, and each bucket is notified at most once per state
history.

When a webhook URL is first configured, the service sends a fresh current-state
snapshot even if it was already running while the URL was blank. Received SMS
records that were not previously acknowledged may also be delivered at that
time; use a blank webhook only when that bootstrap behavior is acceptable.

Call state is derived from ModemManager's active call list. A call is reported
as **possible missed** only when an incoming ringing call disappears before an
active state is seen; the modem/SIM/carrier remains the source of truth.

## Incoming-call policy

`CALL_POLICY=hangup-incoming` is the default: every detected incoming call is
ended through `mmcli -c <call-path> --hangup`. This is intentionally scoped to
incoming calls and does not alter outbound calls. Use `CALL_POLICY=observe` only
for controlled modem/voice-interface development.

The existing DiscordJS project can join a Discord voice channel and play audio,
but it does not contain a modem-audio bridge. A future bridge needs a verified
voice-capable PLSx3 SKU, an exposed bidirectional modem audio interface, and a
dedicated PCM↔Opus conversion pipeline. Do not enable call acceptance before
that integration has been built and tested.

## Public repository and privacy

The repository is intentionally named `cinterion-modem-notifier`. It contains
only source, tests, a sanitized configuration template, and the user service
unit. Do not commit a populated `env` file, ModemManager state, SIM details,
phone numbers, message bodies, webhook URLs, or other runtime artifacts. The
provided `.gitignore` covers the normal local forms of those files, but review
`git diff --cached` before every push.

SMS notifications contain the full sender number and message body by design.
Use a restricted Discord channel and rotate the webhook immediately if it is
ever exposed.

## Install as a user service

```bash
git clone <public-repository-url> ~/cinterion-modem-notifier
mkdir -p ~/.config/cinterion-modem-notifier ~/.config/systemd/user
install -m 600 ~/cinterion-modem-notifier/env.example ~/.config/cinterion-modem-notifier/env
install -m 644 ~/cinterion-modem-notifier/cinterion-modem-notifier.service ~/.config/systemd/user/
# Edit the copied env file and set DISCORD_WEBHOOK_URL only on the local host.
systemctl --user daemon-reload
systemctl --user enable --now cinterion-modem-notifier.service
```

To start automatically after boot while nobody is logged in, an administrator
may run `loginctl enable-linger home` once. Set the webhook URL in the env file,
then run `systemctl --user restart cinterion-modem-notifier`.

## Verify

```bash
cd ~/cinterion-modem-notifier
python3 -m unittest -v
systemctl --user status cinterion-modem-notifier.service
journalctl --user -u cinterion-modem-notifier.service -f
```

The service expects ModemManager, `mmcli`, and `gdbus` to be installed. It uses
only the Python standard library and does not require network access unless a
Discord webhook is configured.

## License

MIT; see [LICENSE](LICENSE).
