# LAN Messenger

A browser-based messenger for computers on the same local network. It supports text, file attachments, separate conversation histories, unread messages, and automatic discovery of computers that join. Files have no application-imposed size limit and are streamed to disk.

The server runs on Ubuntu or Windows using Python's standard library. Clients only need a browser. Messages and files stay on the server; no Internet connection or cloud service is required to use the messenger.

## Run

Requires Python 3.8 or newer.

On Ubuntu:

```bash
python3 lan_messenger.py
```

On Windows, run `Start LAN Messenger.cmd`, or:

```powershell
python lan_messenger.py
```

Open the LAN address printed by the server on each computer. The server's own computer can also use `http://127.0.0.1:8766/`. Choose a computer name, select a recipient, and send text or attach files.

For a specific adapter or network:

```bash
python3 lan_messenger.py --host YOUR_LAN_IP --network YOUR_SUBNET/CIDR
```

## Ubuntu service

Install as an unprivileged service that starts at boot:

```bash
sudo python3 install_ubuntu.py --network YOUR_SUBNET/CIDR
```

The program is installed in `/opt/lan-messenger`. Chat history and the default upload directory are in `/var/lib/lan-messenger`. The installer preserves any existing database by refusing to overwrite it.

```bash
sudo systemctl status lan-messenger
sudo systemctl restart lan-messenger
sudo journalctl -u lan-messenger -n 30 --no-pager
```

If UFW is already active, the installer allows TCP port 8765 from the selected LAN subnet.

## Storage and migration

Manual runs use `messenger-data/history.sqlite3` and `messenger-data/uploads` beside the code. Keep both when backing up or moving a server. File downloads are available only to the computers in that conversation.

`configure_storage.py` contains the current deployment's drive UUID, mount path, and Ubuntu user. Review these values before using it on another machine. It moves existing attachments to the drive, verifies their bytes, and configures a private service bind mount with startup dependencies. The chat database remains in `/var/lib/lan-messenger`.

The private Ubuntu migration flow transfers the database and attachments, preserves computer identities, and pauses the source server to avoid losing newer messages. Detailed deployment and migration instructions are in [LAN Messenger README.md](LAN%20Messenger%20README.md).

This repository contains source and synthetic tests. Runtime chat databases, uploads, logs, and temporary fixtures are excluded.

## Verification

The Python checks use temporary synthetic data, without touching a running messenger:

```bash
python3 verify_identity.py
python3 verify_files.py
python3 verify_ubuntu.py
python3 verify_storage.py
python3 verify_bootstrap_cleanup.py
```

`verify_files.py` checks a streamed 128 MB attachment. `verify_storage.py` also requires filesystem hard-link support.

Optional browser checks require Node.js and Playwright:

```bash
npm install --no-save playwright
npx playwright install chromium
node verify_messenger.cjs
```

Set `PYTHON` if the Python executable has a different name or location. Set `MESSENGER_BROWSER_CHANNEL=chrome` to test an installed Chrome browser. The browser checks launch their own temporary server and verify messaging, file transfers, privacy, restart persistence, and migration controls.

## Network access

This app is intended for a trusted LAN. Each source IP shares a computer inbox, including different browsers on that computer. The host stores the conversations and files. The server validates the allowed subnet, host, request origin, and mutation tokens; it does not provide encrypted transport or public Internet accounts.
