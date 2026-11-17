# A1 Always Free grabber

Small oneshot that runs on the existing Oracle E2.1.Micro and keeps trying
to create an Always Free `VM.Standard.A1.Flex` instance (2 OCPU, 12 GB,
Ubuntu 24.04 aarch64, 50 GB boot, public IPv4) in `eu-milan-1`.

It does **not** migrate MemeDesk. When the instance lands it sends a Telegram
message with the public IP and disables its own timer.

## Safety

- Shape and size are hardcoded. A larger request, a different region, or a
  paid shape will not launch.
- Before each create it lists instances and skips if an A1 already exists.
- 429 TooManyRequests backs off (15 min, then doubles up to 2 h).
- "Out of host capacity" is expected and is retried on the next tick.
- A response that looks like a quota/paid/upgrade error stops the timer and
  alerts on Telegram. The script never upgrades the tenancy to Pay As You Go.
- Secrets live only on the VM in `/etc/memedesk/oci/` (mode 600) and
  `/etc/memedesk/memedesk.env`. They are not in this repo.

## Stop

```bash
sudo systemctl disable --now memedesk-a1grab.timer
```

## Logs

```bash
journalctl -u memedesk-a1grab.service -n 50 --no-pager
sudo tail -50 /var/lib/memedesk/a1grab.log
```
